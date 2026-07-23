"""Normalize acquired text into intermediate blocks used for QA and review."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from glk.application.project_service import load_project
from glk.application.source_review_service import prepare_project_source_review
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock
from glk.domain.workspace import IMAGE_SOURCE_ROOT, PDF_SOURCE_FILE, WorkspacePaths


SEGMENTATION_VERSION = "source-block-v2"
_VOLATILE_ACQUISITION_FIELDS = {"updated_at", "cached_pages", "cached_images"}


class SegmentationError(ValueError):
    """Raised when extracted source cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    project_path: str
    source_type: str
    input_sha256: str
    total_blocks: int
    flagged_blocks: int
    output_file: str | None
    draft_file: str | None = None
    review_file: str | None = None
    review_status: str | None = None
    review_created: bool = False
    cached: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = self.ok
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SegmentationError(f"Required source metadata not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SegmentationError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise SegmentationError(f"Expected a JSON object in {path}")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_hash(text: str) -> str:
    return f"sha256:{_sha256_bytes(text.encode('utf-8'))}"


def _fingerprint_files(project_path: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(project_path).as_posix()):
        relative_name = path.relative_to(project_path).as_posix()
        relative = relative_name.encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        if relative_name in {
            ".glk/state/pdf_acquisition.json",
            ".glk/state/image_ocr.json",
        }:
            try:
                metadata = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SegmentationError(
                    f"Invalid acquisition metadata for fingerprint: {path}"
                ) from error
            if not isinstance(metadata, dict):
                raise SegmentationError(f"Expected a JSON object in {path}")
            stable_metadata = {
                key: value
                for key, value in metadata.items()
                if key not in _VOLATILE_ACQUISITION_FIELDS
            }
            data = json.dumps(
                stable_metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    digest.update(SEGMENTATION_VERSION.encode("utf-8"))
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _serialize_jsonl(blocks: list[SourceBlock]) -> bytes:
    lines = [
        json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":"))
        for block in blocks
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:32] or "source"


def _block_id(
    *,
    source_type: str,
    source_file: str,
    page: int | None,
    block_order: int,
) -> str:
    locator = f"{source_type}|{source_file}|{page or ''}|{block_order}"
    locator_hash = _sha256_bytes(locator.encode("utf-8"))[:10]
    if source_type == "pdf":
        prefix = f"pdf-p{page:04d}"
    else:
        prefix = f"image-{_slug(PurePosixPath(source_file).stem)}"
    return f"{prefix}-b{block_order:04d}-{locator_hash}"


def _union_pdf_bbox(
    fragment_ids: list[str],
    fragments: dict[str, dict[str, Any]],
    page_size: list[Any],
) -> tuple[float, float, float, float]:
    if (
        not isinstance(page_size, list)
        or len(page_size) != 2
        or not all(isinstance(value, (int, float)) for value in page_size)
        or float(page_size[0]) <= 0
        or float(page_size[1]) <= 0
    ):
        raise SegmentationError("PDF fragment metadata has an invalid page_size.")
    boxes: list[list[Any]] = []
    for fragment_id in fragment_ids:
        fragment = fragments.get(fragment_id)
        if fragment is None:
            raise SegmentationError(f"Unknown PDF fragment ID: {fragment_id}")
        bbox = fragment.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            raise SegmentationError(f"Fragment {fragment_id} has an invalid bbox.")
        boxes.append(bbox)
    if not boxes:
        raise SegmentationError("PDF source block has no fragment IDs.")
    width, height = float(page_size[0]), float(page_size[1])
    raw = (
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    )
    normalized = (
        max(0.0, min(1000.0, raw[0] / width * 1000)),
        max(0.0, min(1000.0, raw[1] / height * 1000)),
        max(0.0, min(1000.0, raw[2] / width * 1000)),
        max(0.0, min(1000.0, raw[3] / height * 1000)),
    )
    return tuple(round(value, 2) for value in normalized)


def _validate_complete_run(metadata: dict[str, Any], path: Path) -> None:
    if metadata.get("status") != "complete" or metadata.get("failures"):
        raise SegmentationError(
            f"Source acquisition is not complete according to {path}; "
            "resolve failures before segmentation."
        )


def _build_pdf_blocks(project_path: Path) -> tuple[list[SourceBlock], list[Path]]:
    paths = WorkspacePaths(project_path)
    document_path = paths.pdf_acquisition_state
    document = _read_json(document_path)
    _validate_complete_run(document, document_path)
    pages = document.get("successful_pages")
    if not isinstance(pages, list) or not pages:
        raise SegmentationError("PDF document metadata has no successful pages.")
    blocks: list[SourceBlock] = []
    input_paths = [document_path]
    source_order = 0
    for page_value in pages:
        if not isinstance(page_value, int) or isinstance(page_value, bool) or page_value <= 0:
            raise SegmentationError(f"Invalid successful page number: {page_value!r}")
        page = page_value
        stem = f"page_{page:03d}"
        layout_path = paths.pdf_layouts / f"{stem}.json"
        fragment_path = paths.pdf_fragments / f"{stem}.json"
        layout = _read_json(layout_path)
        fragment_data = _read_json(fragment_path)
        input_paths.extend((layout_path, fragment_path))
        reconstructed = layout.get("reconstructed_blocks")
        fragment_values = fragment_data.get("fragments")
        if not isinstance(reconstructed, list) or not isinstance(fragment_values, list):
            raise SegmentationError(f"Invalid PDF layout data for page {page}.")
        fragments = {
            value.get("id"): value
            for value in fragment_values
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        }
        for block_order, value in enumerate(reconstructed, start=1):
            if not isinstance(value, dict):
                raise SegmentationError(f"Invalid reconstructed block on page {page}.")
            if value.get("include_in_text") is not True:
                continue
            text = value.get("text")
            fragment_ids = value.get("fragment_ids")
            if not isinstance(text, str) or not text.strip():
                raise SegmentationError(f"Empty reconstructed block on page {page}.")
            if not isinstance(fragment_ids, list) or not all(
                isinstance(item, str) for item in fragment_ids
            ):
                raise SegmentationError(f"Invalid fragment references on page {page}.")
            source_order += 1
            raw_text = text.strip()
            block = SourceBlock(
                schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
                id=_block_id(
                    source_type="pdf",
                    source_file=PDF_SOURCE_FILE,
                    page=page,
                    block_order=block_order,
                ),
                source_type="pdf",
                source_file=PDF_SOURCE_FILE,
                page=page,
                source_order=source_order,
                block_order=block_order,
                block_type=str(value.get("type") or "other"),
                raw_text=raw_text,
                corrected_text=None,
                bbox=_union_pdf_bbox(
                    fragment_ids, fragments, fragment_data.get("page_size")
                ),
                legibility=None,
                status="raw",
                warnings=(),
                source_refs=tuple(fragment_ids),
                source_hash=_source_hash(raw_text),
            )
            block.validate()
            blocks.append(block)
    if not blocks:
        raise SegmentationError("PDF extraction contains no included source blocks.")
    return blocks, input_paths


def _safe_image_relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SegmentationError("Image OCR summary contains an invalid source path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SegmentationError(f"Unsafe image source path: {value}")
    return path


def _normalized_image_bbox(value: Any) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(item, (int, float)) for item in value)
    ):
        raise SegmentationError("Image OCR block has an invalid bbox.")
    return tuple(round(float(item), 2) for item in value)


def _build_image_blocks(project_path: Path) -> tuple[list[SourceBlock], list[Path]]:
    paths = WorkspacePaths(project_path)
    summary_path = paths.image_ocr_state
    summary = _read_json(summary_path)
    _validate_complete_run(summary, summary_path)
    images = summary.get("successful_images")
    if not isinstance(images, list) or not images:
        raise SegmentationError("Image OCR summary has no successful images.")
    blocks: list[SourceBlock] = []
    input_paths = [summary_path]
    source_order = 0
    for image_value in images:
        relative = _safe_image_relative(image_value)
        result_path = paths.ocr_results / Path(
            *relative.with_suffix(".json").parts
        )
        result = _read_json(result_path)
        input_paths.append(result_path)
        ocr = result.get("ocr")
        if not isinstance(ocr, dict) or not isinstance(ocr.get("blocks"), list):
            raise SegmentationError(f"Invalid OCR result: {result_path}")
        warnings_value = ocr.get("warnings", [])
        if not isinstance(warnings_value, list) or not all(
            isinstance(item, str) for item in warnings_value
        ):
            raise SegmentationError(f"Invalid OCR warnings: {result_path}")
        source_file = result.get("source_image")
        _safe_image_relative(source_file)
        expected_source_file = f"{IMAGE_SOURCE_ROOT}/{relative.as_posix()}"
        if source_file != expected_source_file:
            raise SegmentationError(
                f"OCR result source mismatch: expected {expected_source_file}, "
                f"got {source_file}"
            )
        for block_order, value in enumerate(ocr["blocks"], start=1):
            if not isinstance(value, dict):
                raise SegmentationError(f"Invalid OCR block: {result_path}")
            text = value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SegmentationError(f"Empty OCR block: {result_path}")
            legibility = value.get("legibility")
            status = (
                "flagged"
                if legibility == "uncertain" or warnings_value
                else "raw"
            )
            source_order += 1
            raw_text = text.strip()
            block = SourceBlock(
                schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
                id=_block_id(
                    source_type="image",
                    source_file=source_file,
                    page=None,
                    block_order=block_order,
                ),
                source_type="image",
                source_file=source_file,
                page=None,
                source_order=source_order,
                block_order=block_order,
                block_type=str(value.get("type") or "other"),
                raw_text=raw_text,
                corrected_text=None,
                bbox=_normalized_image_bbox(value.get("bbox")),
                legibility=legibility,
                status=status,
                warnings=tuple(warnings_value),
                source_refs=(),
                source_hash=_source_hash(raw_text),
            )
            block.validate()
            blocks.append(block)
    if not blocks:
        raise SegmentationError("Image OCR contains no source blocks.")
    return blocks, input_paths


def _load_cached_result(
    *,
    state_path: Path,
    output_path: Path,
    input_sha256: str,
    source_type: str,
) -> SegmentationResult | None:
    if not state_path.is_file() or not output_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        output_hash = _sha256_bytes(output_path.read_bytes())
        if not (
            state.get("status") == "complete"
            and state.get("version") == SEGMENTATION_VERSION
            and state.get("input_sha256") == input_sha256
            and state.get("source_type") == source_type
            and state.get("output_sha256") == output_hash
        ):
            return None
        return SegmentationResult(
            project_path=str(state_path.parents[2]),
            source_type=source_type,
            input_sha256=input_sha256,
            total_blocks=int(state["total_blocks"]),
            flagged_blocks=int(state["flagged_blocks"]),
            output_file=str(output_path),
            cached=True,
        )
    except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def segment_project_source(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    dry_run: bool = False,
) -> SegmentationResult:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    if location.manifest.source_file == PDF_SOURCE_FILE:
        source_type = "pdf"
        blocks, input_paths = _build_pdf_blocks(location.path)
    elif location.manifest.source_file == IMAGE_SOURCE_ROOT:
        source_type = "image"
        blocks, input_paths = _build_image_blocks(location.path)
    else:
        raise SegmentationError(
            "Project has no supported registered source; run glk extract or glk ocr first."
        )
    input_hash = _fingerprint_files(location.path, input_paths)
    output_path = paths.source_segments
    state_path = paths.segmentation_state
    manifest_path = paths.source_manifest
    flagged_count = sum(block.status == "flagged" for block in blocks)
    if dry_run:
        return SegmentationResult(
            project_path=str(location.path),
            source_type=source_type,
            input_sha256=input_hash,
            total_blocks=len(blocks),
            flagged_blocks=flagged_count,
            output_file=None,
            dry_run=True,
        )
    if not force:
        cached = _load_cached_result(
            state_path=state_path,
            output_path=output_path,
            input_sha256=input_hash,
            source_type=source_type,
        )
        if cached is not None:
            review = prepare_project_source_review(project=location.path)
            return replace(
                cached,
                draft_file=review.draft_file,
                review_file=review.review_file,
                review_status=review.review_status,
                review_created=review.review_created,
            )
    output_bytes = _serialize_jsonl(blocks)
    _write_bytes_atomic(output_path, output_bytes)
    output_hash = _sha256_bytes(output_bytes)
    state = {
        "schema_version": 1,
        "status": "complete",
        "version": SEGMENTATION_VERSION,
        "source_type": source_type,
        "source_file": location.manifest.source_file,
        "input_sha256": input_hash,
        "block_schema_version": SOURCE_BLOCK_SCHEMA_VERSION,
        "total_blocks": len(blocks),
        "flagged_blocks": flagged_count,
        "output_file": paths.relative(paths.source_segments),
        "output_sha256": output_hash,
        "updated_at": _utc_now(),
    }
    _write_json_atomic(manifest_path, state)
    _write_json_atomic(state_path, state)
    review = prepare_project_source_review(project=location.path)
    return SegmentationResult(
        project_path=str(location.path),
        source_type=source_type,
        input_sha256=input_hash,
        total_blocks=len(blocks),
        flagged_blocks=flagged_count,
        output_file=str(output_path),
        draft_file=review.draft_file,
        review_file=review.review_file,
        review_status=review.review_status,
        review_created=review.review_created,
    )
