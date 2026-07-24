"""Project-level PDF extraction and LLM layout orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pymupdf

from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._hashing import sha256_file as _sha256_file
from glk.application._io import copy_file_atomic as _copy_source_atomic
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application._io import write_text_atomic as _write_text_atomic
from glk.application.project_service import (
    ProjectLocation,
    load_project,
    update_project_source,
)
from glk.domain.workspace import WorkspacePaths, is_pdf_source_file
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


ProgressCallback = Callable[[str], None]
LAYOUT_VALIDATION_ATTEMPTS = 3


class ExtractionError(ValueError):
    """Raised when a project source cannot be registered or extracted."""


@dataclass(frozen=True, slots=True)
class PageFailure:
    page: int
    error: str


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


def _register_source(
    location: ProjectLocation, source_path: Path, force: bool
) -> tuple[ProjectLocation, Path, str]:
    paths = WorkspacePaths(location.path)
    input_dir = paths.input_pdf_dir.resolve()
    destination = (
        source_path
        if source_path.parent == input_dir
        else input_dir / source_path.name
    )
    source_file = paths.relative(destination)
    current_source = location.manifest.source_file
    if current_source and current_source != source_file and not force:
        raise ExtractionError(
            f"Project source is already registered as {current_source}. "
            "Use --force to replace it."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = _sha256_file(source_path)
    same_path = source_path == destination.resolve()
    if destination.exists() and not same_path:
        destination_hash = _sha256_file(destination)
        if destination_hash != source_hash and not force:
            raise ExtractionError(
                f"A different {source_file} is already registered. "
                "Use --force to replace the project source."
            )
        if destination_hash != source_hash or force:
            _copy_source_atomic(source_path, destination)
    elif not destination.exists():
        _copy_source_atomic(source_path, destination)
    registered_hash = _sha256_file(destination)
    if registered_hash != source_hash:
        raise ExtractionError("Registered PDF hash does not match the input PDF.")
    if (
        force
        and current_source != source_file
        and is_pdf_source_file(current_source)
    ):
        previous_source = location.path / str(current_source)
        if previous_source.is_file():
            previous_source.unlink()
    if current_source != source_file:
        location = update_project_source(location, source_file)
    return location, destination, registered_hash


def _load_cached_layout(
    path: Path,
    *,
    source_sha256: str,
    fragment_sha256: str,
    provider: LayoutProvider,
    fragments: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
    except (KeyError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


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


def extract_project_pdf(
    *,
    project: str | Path,
    file: str | Path | None = None,
    pages: str | None = None,
    workspace_root: str | Path = "workspaces",
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

    active_provider = provider or GeminiLayoutProvider.from_environment(model_name)
    location, registered_source, source_hash = _register_source(location, source_path, force)
    paths = WorkspacePaths(location.path)
    pages_dir = paths.pdf_pages
    fragments_dir = paths.pdf_fragments
    layouts_dir = paths.pdf_layouts
    for directory in (pages_dir, fragments_dir, layouts_dir, paths.state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    document = pymupdf.open(registered_source)
    successful_text: dict[int, str] = {}
    successful_pages: list[int] = []
    cached_pages: list[int] = []
    failures: list[PageFailure] = []
    try:
        page_indexes = parse_page_selection(pages, document.page_count)
        for page_index in page_indexes:
            page_number = page_index + 1
            notify(f"Page {page_number}: extracting PDF fragments")
            try:
                page = document[page_index]
                fragments = extract_line_fragments(page, page_number)
                if not fragments:
                    raise ExtractionError(
                        "No embedded text fragments were found; this page requires OCR."
                    )
                png_bytes, page_image = render_page(page, scale)
                fragment_hash = _sha256_json(fragments)
                page_stem = f"page_{page_number:03d}"
                _write_bytes_atomic(pages_dir / f"{page_stem}.png", png_bytes)
                _write_json_atomic(
                    fragments_dir / f"{page_stem}.json",
                    {
                        "schema_version": 1,
                        "source_sha256": source_hash,
                        "page": page_number,
                        "page_size": [round(page.rect.width, 2), round(page.rect.height, 2)],
                        "fragment_sha256": fragment_hash,
                        "fragments": fragments,
                    },
                )
                layout_path = layouts_dir / f"{page_stem}.json"
                cached = None if force else _load_cached_layout(
                    layout_path,
                    source_sha256=source_hash,
                    fragment_sha256=fragment_hash,
                    provider=active_provider,
                    fragments=fragments,
                )
                if cached is not None:
                    layout, blocks = cached
                    validation = validate_layout(fragments, layout)
                    cached_pages.append(page_number)
                    notify(f"Page {page_number}: reused validated layout cache")
                else:
                    notify(f"Page {page_number}: requesting LLM layout reconstruction")
                    layout, validation = _reconstruct_validated_layout(
                        page_number=page_number,
                        fragments=fragments,
                        page_image=page_image,
                        provider=active_provider,
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
                        "model": active_provider.model_name,
                        "prompt_version": active_provider.prompt_version,
                        "postprocess_version": POSTPROCESS_VERSION,
                        "validation": validation,
                        "layout": layout,
                        "reconstructed_blocks": blocks,
                    },
                )
                page_text = build_page_text(blocks)
                _write_text_atomic(layouts_dir / f"{page_stem}.txt", page_text)
                successful_text[page_number] = page_text
                successful_pages.append(page_number)
            except Exception as error:
                failures.append(PageFailure(page_number, str(error)))
                notify(f"Page {page_number}: failed: {error}")
    finally:
        document.close()

    combined = "\n\n".join(
        f"[PAGE {page_number}]\n{successful_text[page_number]}"
        for page_number in successful_pages
    )
    output_path = paths.source_extracted
    if failures:
        output_path = paths.source_extracted_partial
    _write_text_atomic(output_path, combined)
    run_status = {
        "schema_version": 1,
        "status": "complete" if not failures else "partial",
        "source_file": location.manifest.source_file,
        "source_sha256": source_hash,
        "page_count": page_count,
        "selected_pages": list(selected_pages),
        "successful_pages": successful_pages,
        "cached_pages": cached_pages,
        "failures": [asdict(failure) for failure in failures],
        "model": active_provider.model_name,
        "prompt_version": active_provider.prompt_version,
        "output_file": str(output_path.relative_to(location.path)),
        "updated_at": _utc_now(),
    }
    _write_json_atomic(paths.pdf_acquisition_state, run_status)
    return ExtractionResult(
        project_path=str(location.path),
        source_pdf=str(registered_source),
        source_sha256=source_hash,
        model=active_provider.model_name,
        prompt_version=active_provider.prompt_version,
        selected_pages=selected_pages,
        successful_pages=tuple(successful_pages),
        cached_pages=tuple(cached_pages),
        failures=tuple(failures),
        output_file=str(output_path),
    )
