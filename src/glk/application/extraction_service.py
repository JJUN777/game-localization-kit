"""Project-level PDF extraction and LLM layout orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pymupdf

from glk.application._cache import invalid_cache, read_json_object
from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._hashing import sha256_file as _sha256_file
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application._io import write_text_atomic as _write_text_atomic
from glk.application.project_service import (
    ProjectLocation,
    load_project,
)
from glk.application.source_registration_service import (
    SourceRegistrationError,
    register_pdf_source,
)
from glk.domain.workspace import WorkspacePaths
from glk.extraction.layout import (
    LayoutValidationError,
    LayoutProvider,
    POSTPROCESS_VERSION,
    build_page_text,
    extract_line_fragments,
    merge_paragraph_continuations,
    parse_page_selection,
    reconstruct_blocks,
    render_page,
    validate_layout,
)
from glk.infrastructure.gemini_layout import GeminiLayoutProvider
from glk.infrastructure.gemini_common import gemini_failure_code


ProgressCallback = Callable[[str], None]
LAYOUT_VALIDATION_ATTEMPTS = 3


class ExtractionError(ValueError):
    """Raised when a project source cannot be registered or extracted."""


@dataclass(frozen=True, slots=True)
class PageFailure:
    page: int
    error: str
    code: str = "SOURCE_PROCESSING_FAILED"


@dataclass(frozen=True, slots=True)
class _PageExtraction:
    page: int
    text: str
    cached: bool


@dataclass(frozen=True, slots=True)
class _PageExtractionBatch:
    successful: tuple[_PageExtraction, ...]
    failures: tuple[PageFailure, ...]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    project_path: str
    source_pdf: str
    source_sha256: str
    model: str | None
    prompt_version: str | None
    selected_pages: tuple[int, ...]
    successful_pages: tuple[int, ...]
    cached_pages: tuple[int, ...]
    failures: tuple[PageFailure, ...]
    output_file: str | None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = self.ok
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_file(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ExtractionError(f"PDF not found: {candidate}")
    if candidate.suffix.casefold() != ".pdf":
        raise ExtractionError(f"Source must be a PDF file: {candidate}")
    return candidate


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _resolve_project_source(
    location: ProjectLocation, file: str | Path | None
) -> Path:
    if file is not None:
        return _resolve_file(file)
    if not location.manifest.source_file:
        raise ExtractionError("No source PDF is registered; provide --file.")
    return _resolve_file(location.path / location.manifest.source_file)


def _load_cached_layout(
    path: Path,
    *,
    source_sha256: str,
    fragment_sha256: str,
    provider: LayoutProvider,
    fragments: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    value = read_json_object(path)
    if value is None:
        return None
    try:
        metadata_matches = (
            value.get("source_sha256") == source_sha256
            and value.get("fragment_sha256") == fragment_sha256
            and value.get("model") == provider.model_name
            and value.get("prompt_version") == provider.prompt_version
        )
        if not metadata_matches:
            return None
        layout = value["layout"]
        validate_layout(fragments, layout)
        return layout, merge_paragraph_continuations(reconstruct_blocks(fragments, layout))
    except (KeyError, ValueError, TypeError) as error:
        raise invalid_cache(path, "invalid PDF layout") from error


def _reconstruct_validated_layout(
    *,
    page_number: int,
    fragments: list[dict[str, Any]],
    page_image: Any,
    provider: LayoutProvider,
    notify: ProgressCallback,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry only structurally valid LLM responses without repairing source IDs."""
    for attempt in range(1, LAYOUT_VALIDATION_ATTEMPTS + 1):
        layout = provider.reconstruct(page_number, fragments, page_image)
        try:
            return layout, validate_layout(fragments, layout)
        except LayoutValidationError as error:
            if attempt == LAYOUT_VALIDATION_ATTEMPTS:
                raise
            notify(
                f"Page {page_number}: layout fragment validation failed; "
                f"retrying LLM reconstruction ({attempt + 1}/"
                f"{LAYOUT_VALIDATION_ATTEMPTS}): {error}"
            )
    raise RuntimeError("Layout validation retry loop ended unexpectedly.")


def _extract_pdf_page(
    *,
    page: Any,
    page_number: int,
    source_hash: str,
    paths: WorkspacePaths,
    provider: LayoutProvider,
    scale: float,
    force: bool,
    notify: ProgressCallback,
) -> _PageExtraction:
    notify(f"Page {page_number}: extracting PDF fragments")
    fragments = extract_line_fragments(page, page_number)
    if not fragments:
        raise ExtractionError(
            "No embedded text fragments were found; this page requires OCR."
        )
    png_bytes, page_image = render_page(page, scale)
    fragment_hash = _sha256_json(fragments)
    page_stem = f"page_{page_number:03d}"
    _write_bytes_atomic(paths.pdf_pages / f"{page_stem}.png", png_bytes)
    _write_json_atomic(
        paths.pdf_fragments / f"{page_stem}.json",
        {
            "schema_version": 1,
            "source_sha256": source_hash,
            "page": page_number,
            "page_size": [
                round(page.rect.width, 2),
                round(page.rect.height, 2),
            ],
            "fragment_sha256": fragment_hash,
            "fragments": fragments,
        },
    )

    layout_path = paths.pdf_layouts / f"{page_stem}.json"
    cached = None if force else _load_cached_layout(
        layout_path,
        source_sha256=source_hash,
        fragment_sha256=fragment_hash,
        provider=provider,
        fragments=fragments,
    )
    if cached is not None:
        layout, blocks = cached
        validation = validate_layout(fragments, layout)
        notify(f"Page {page_number}: reused validated layout cache")
    else:
        notify(f"Page {page_number}: requesting LLM layout reconstruction")
        layout, validation = _reconstruct_validated_layout(
            page_number=page_number,
            fragments=fragments,
            page_image=page_image,
            provider=provider,
            notify=notify,
        )
        blocks = merge_paragraph_continuations(
            reconstruct_blocks(fragments, layout)
        )
    _write_json_atomic(
        layout_path,
        {
            "schema_version": 1,
            "source_sha256": source_hash,
            "fragment_sha256": fragment_hash,
            "page": page_number,
            "model": provider.model_name,
            "prompt_version": provider.prompt_version,
            "postprocess_version": POSTPROCESS_VERSION,
            "validation": validation,
            "layout": layout,
            "reconstructed_blocks": blocks,
        },
    )
    page_text = build_page_text(blocks)
    _write_text_atomic(paths.pdf_layouts / f"{page_stem}.txt", page_text)
    return _PageExtraction(page_number, page_text, cached is not None)


def _extract_selected_pages(
    *,
    source_path: Path,
    page_indexes: list[int],
    source_hash: str,
    paths: WorkspacePaths,
    provider: LayoutProvider,
    scale: float,
    force: bool,
    notify: ProgressCallback,
) -> _PageExtractionBatch:
    successful: list[_PageExtraction] = []
    failures: list[PageFailure] = []
    document = pymupdf.open(source_path)
    try:
        for page_index in page_indexes:
            page_number = page_index + 1
            try:
                successful.append(
                    _extract_pdf_page(
                        page=document[page_index],
                        page_number=page_number,
                        source_hash=source_hash,
                        paths=paths,
                        provider=provider,
                        scale=scale,
                        force=force,
                        notify=notify,
                    )
                )
            except Exception as error:
                failures.append(
                    PageFailure(
                        page_number,
                        str(error),
                        gemini_failure_code(error),
                    )
                )
                notify(f"Page {page_number}: failed: {error}")
    finally:
        document.close()
    return _PageExtractionBatch(tuple(successful), tuple(failures))


def _write_extraction_result(
    *,
    location: ProjectLocation,
    paths: WorkspacePaths,
    source_hash: str,
    page_count: int,
    selected_pages: tuple[int, ...],
    batch: _PageExtractionBatch,
    provider: LayoutProvider,
) -> Path:
    successful_pages = [item.page for item in batch.successful]
    cached_pages = [item.page for item in batch.successful if item.cached]
    combined = "\n\n".join(
        f"[PAGE {item.page}]\n{item.text}" for item in batch.successful
    )
    output_path = (
        paths.source_extracted_partial
        if batch.failures
        else paths.source_extracted
    )
    _write_text_atomic(output_path, combined)
    _write_json_atomic(
        paths.pdf_acquisition_state,
        {
            "schema_version": 1,
            "status": "partial" if batch.failures else "complete",
            "source_file": location.manifest.source_file,
            "source_sha256": source_hash,
            "page_count": page_count,
            "selected_pages": list(selected_pages),
            "successful_pages": successful_pages,
            "cached_pages": cached_pages,
            "failures": [asdict(failure) for failure in batch.failures],
            "model": provider.model_name,
            "prompt_version": provider.prompt_version,
            "output_file": str(output_path.relative_to(location.path)),
            "updated_at": _utc_now(),
        },
    )
    return output_path


def extract_project_pdf(
    *,
    project: str | Path,
    file: str | Path | None = None,
    pages: str | None = None,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    model_name: str | None = None,
    scale: float = 1.5,
    force: bool = False,
    dry_run: bool = False,
    provider: LayoutProvider | None = None,
    progress: ProgressCallback | None = None,
) -> ExtractionResult:
    if scale <= 0:
        raise ExtractionError("Render scale must be greater than zero.")
    notify = progress or (lambda _: None)
    location = load_project(project, workspace_root)
    source_path = _resolve_project_source(location, file)
    source_hash = _sha256_file(source_path)

    document = pymupdf.open(source_path)
    try:
        page_count = document.page_count
        page_indexes = parse_page_selection(pages, page_count)
    finally:
        document.close()
    selected_pages = tuple(index + 1 for index in page_indexes)
    if dry_run:
        return ExtractionResult(
            project_path=str(location.path),
            source_pdf=str(source_path),
            source_sha256=source_hash,
            model=model_name,
            prompt_version=None,
            selected_pages=selected_pages,
            successful_pages=(),
            cached_pages=(),
            failures=(),
            output_file=None,
            dry_run=True,
        )

    active_provider = provider or GeminiLayoutProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )
    try:
        registered = register_pdf_source(
            location,
            source_path,
            force=force,
        )
    except SourceRegistrationError as error:
        raise ExtractionError(str(error)) from error
    location = registered.location
    registered_source = registered.path
    source_hash = registered.sha256
    paths = WorkspacePaths(location.path)
    for directory in (
        paths.pdf_pages,
        paths.pdf_fragments,
        paths.pdf_layouts,
        paths.state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    batch = _extract_selected_pages(
        source_path=registered_source,
        page_indexes=page_indexes,
        source_hash=source_hash,
        paths=paths,
        provider=active_provider,
        scale=scale,
        force=force,
        notify=notify,
    )
    output_path = _write_extraction_result(
        location=location,
        paths=paths,
        source_hash=source_hash,
        page_count=page_count,
        selected_pages=selected_pages,
        batch=batch,
        provider=active_provider,
    )
    return ExtractionResult(
        project_path=str(location.path),
        source_pdf=str(registered_source),
        source_sha256=source_hash,
        model=active_provider.model_name,
        prompt_version=active_provider.prompt_version,
        selected_pages=selected_pages,
        successful_pages=tuple(item.page for item in batch.successful),
        cached_pages=tuple(
            item.page for item in batch.successful if item.cached
        ),
        failures=batch.failures,
        output_file=str(output_path),
    )
