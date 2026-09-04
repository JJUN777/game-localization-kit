"""Audit selected PDF source blocks for meaningful icons omitted from text."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from glk.application._io import write_json_atomic
from glk.application.ai_usage_ledger import append_ai_usage_event
from glk.application.project_service import load_project
from glk.application.review_types import SourceReviewBlock
from glk.application.source_review_service import (
    SourceReviewError,
    get_project_source_review_document,
)
from glk.domain.workspace import WorkspacePaths
from glk.extraction.pdf_icon_audit import (
    PDF_ICON_AUDIT_PROMPT_VERSION,
    build_pdf_icon_audit_prompt,
    icon_token_definitions,
    insert_icon_markers,
    validate_pdf_icon_audit_result,
)
from glk.infrastructure.ai_provider import (
    create_pdf_icon_audit_provider,
    resolve_ai_model_name,
    resolve_ai_provider_name,
)
from glk.infrastructure.ai_usage import provider_usage, usage_delta


MAX_ICON_AUDIT_BLOCKS = 24
MAX_ICON_AUDIT_TEXT_CHARS = 12_000
ICON_CROP_PADDING = 24.0
PDF_ICON_AUDIT_CACHE_VERSION = 1


class PdfIconAuditError(SourceReviewError):
    """Raised when selected PDF blocks cannot be inspected safely."""

    code = "PDF_ICON_AUDIT_FAILED"


class PdfIconAuditProvider(Protocol):
    model_name: str

    def inspect(self, prompt: str, image: Image.Image) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PdfIconAuditBlockResult:
    block_id: str
    group_id: str
    page: int
    current_text: str
    suggested_text: str
    icons: list[dict[str, Any]]
    summary: str
    crop_url: str
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PdfIconAuditResult:
    results: list[PdfIconAuditBlockResult]
    inspected_blocks: int
    detected_icons: int
    cached_blocks: int
    usage: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "inspected_blocks": self.inspected_blocks,
            "detected_icons": self.detected_icons,
            "cached_blocks": self.cached_blocks,
            "usage": self.usage,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_cache(path: Path) -> dict[str, Any]:
    empty = {"schema_version": PDF_ICON_AUDIT_CACHE_VERSION, "blocks": {}}
    if not path.is_file():
        return empty
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return empty
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PDF_ICON_AUDIT_CACHE_VERSION
        or not isinstance(value.get("blocks"), dict)
    ):
        return empty
    return value


def _provider_identity(
    provider: PdfIconAuditProvider | None,
) -> tuple[str, str]:
    if provider is None:
        provider_name = resolve_ai_provider_name()
        return provider_name, resolve_ai_model_name(provider_name=provider_name)
    usage = provider_usage(provider)
    injected_name = usage.get("provider") if usage else None
    if not isinstance(injected_name, str) or not injected_name:
        injected_name = type(provider).__module__.rsplit(".", 1)[-1]
    return injected_name, provider.model_name


def _audit_fingerprint(
    *,
    block: SourceReviewBlock,
    text: str,
    crop: Image.Image,
    definitions: dict[str, str],
    provider_name: str,
    model_name: str,
) -> str:
    digest = hashlib.sha256()
    metadata = {
        "prompt_version": PDF_ICON_AUDIT_PROMPT_VERSION,
        "provider": provider_name,
        "model": model_name,
        "block_id": block["id"],
        "page": block["page"],
        "bbox": block["bbox"],
        "text": text,
        "token_definitions": definitions,
        "image_mode": crop.mode,
        "image_size": crop.size,
    }
    digest.update(
        json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(crop.tobytes())
    return "sha256:" + digest.hexdigest()


def _load_prompt_definitions(paths: WorkspacePaths) -> dict[str, str]:
    if not paths.input_ocr_prompt.is_file():
        return {}
    try:
        prompt = paths.input_ocr_prompt.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise PdfIconAuditError("OCR prompt is not valid UTF-8.") from error
    return icon_token_definitions(prompt)


def _crop_box(
    bbox: list[float],
    *,
    width: int,
    height: int,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    if (
        len(bbox) != 4
        or not all(isinstance(value, (int, float)) for value in bbox)
    ):
        raise PdfIconAuditError("Selected PDF block has no valid bounding box.")
    x0, y0, x1, y1 = (float(value) for value in bbox)
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        raise PdfIconAuditError("Selected PDF block bounding box is invalid.")
    block = (
        round(x0 / 1000 * width),
        round(y0 / 1000 * height),
        round(x1 / 1000 * width),
        round(y1 / 1000 * height),
    )
    pad_x = max(12, round(ICON_CROP_PADDING / 1000 * width))
    pad_y = max(12, round(ICON_CROP_PADDING / 1000 * height))
    crop = (
        max(0, block[0] - pad_x),
        max(0, block[1] - pad_y),
        min(width, block[2] + pad_x),
        min(height, block[3] + pad_y),
    )
    target = (
        block[0] - crop[0],
        block[1] - crop[1],
        block[2] - crop[0],
        block[3] - crop[1],
    )
    return crop, target


def crop_pdf_review_block(
    *,
    project: str | Path,
    workspace_root: str | Path,
    block_id: str,
) -> Image.Image:
    """Return a padded crop for a persisted PDF review block."""
    document = get_project_source_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    block = next((item for item in document["blocks"] if item["id"] == block_id), None)
    if (
        block is None
        or block["source_type"] != "pdf"
        or block["page"] is None
        or block["bbox"] is None
        or block["manual"]
    ):
        raise PdfIconAuditError("Selected PDF block cannot be inspected.")
    location = load_project(project, workspace_root)
    page_path = WorkspacePaths(location.path).pdf_pages / f"page_{block['page']:03d}.png"
    if not page_path.is_file():
        raise PdfIconAuditError("Rendered PDF page is missing.")
    with Image.open(page_path) as page_image:
        source = page_image.convert("RGB")
    crop, _ = _crop_box(block["bbox"], width=source.width, height=source.height)
    return source.crop(crop)


def audit_project_pdf_icons(
    *,
    project: str | Path,
    workspace_root: str | Path,
    expected_review_sha256: str,
    selected_blocks: list[dict[str, Any]],
    provider: PdfIconAuditProvider | None = None,
) -> PdfIconAuditResult:
    """Inspect selected persisted blocks while accepting current browser text."""
    if not selected_blocks or len(selected_blocks) > MAX_ICON_AUDIT_BLOCKS:
        raise PdfIconAuditError(
            f"Select between 1 and {MAX_ICON_AUDIT_BLOCKS} PDF blocks."
        )
    document = get_project_source_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    if document["source_type"] != "pdf":
        raise PdfIconAuditError("PDF icon inspection is available only for PDF sources.")
    if document["review_sha256"] != expected_review_sha256:
        raise PdfIconAuditError("Source review changed; reload before inspecting icons.")
    by_id = {block["id"]: block for block in document["blocks"]}
    seen: set[str] = set()
    normalized: list[tuple[SourceReviewBlock, str]] = []
    for selected in selected_blocks:
        if not isinstance(selected, dict):
            raise PdfIconAuditError("Selected blocks must be objects.")
        block_id = selected.get("id")
        text = selected.get("text")
        if not isinstance(block_id, str) or not isinstance(text, str):
            raise PdfIconAuditError("Every selected block requires id and text.")
        if block_id in seen:
            raise PdfIconAuditError("The same PDF block was selected more than once.")
        seen.add(block_id)
        review_block = by_id.get(block_id)
        if (
            review_block is None
            or review_block["source_type"] != "pdf"
            or review_block["page"] is None
            or review_block["bbox"] is None
            or review_block["manual"]
            or review_block["excluded"]
        ):
            raise PdfIconAuditError(f"Block {block_id} cannot be inspected.")
        if not text.strip() or len(text) > MAX_ICON_AUDIT_TEXT_CHARS:
            raise PdfIconAuditError(f"Block {block_id} text is invalid.")
        normalized.append((review_block, text))

    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    definitions = _load_prompt_definitions(paths)
    provider_name, model_name = _provider_identity(provider)
    cache = _load_cache(paths.pdf_icon_audit_state)
    cache_blocks = cache["blocks"]
    assert isinstance(cache_blocks, dict)
    active_provider = provider
    usage_before = provider_usage(active_provider) if active_provider else None
    results: list[PdfIconAuditBlockResult] = []
    cached_blocks = 0
    for target_block, text in normalized:
        page = target_block["page"]
        assert isinstance(page, int)
        page_path = paths.pdf_pages / f"page_{page:03d}.png"
        if not page_path.is_file():
            raise PdfIconAuditError(f"Rendered PDF page {page} is missing.")
        with Image.open(page_path) as page_image:
            source = page_image.convert("RGB")
        bbox = target_block["bbox"]
        assert bbox is not None
        crop_box, target_box = _crop_box(
            bbox,
            width=source.width,
            height=source.height,
        )
        crop = source.crop(crop_box)
        fingerprint = _audit_fingerprint(
            block=target_block,
            text=text,
            crop=crop,
            definitions=definitions,
            provider_name=provider_name,
            model_name=model_name,
        )
        cached = False
        cached_entry = cache_blocks.get(target_block["id"])
        validated: dict[str, Any] | None = None
        if (
            isinstance(cached_entry, dict)
            and cached_entry.get("fingerprint") == fingerprint
        ):
            try:
                validated = validate_pdf_icon_audit_result(
                    {
                        "icons": cached_entry.get("icons"),
                        "summary": cached_entry.get("summary"),
                    },
                    text=text,
                    token_definitions=definitions,
                )
            except (PdfIconAuditError, TypeError, ValueError):
                validated = None
            else:
                cached = True
                cached_blocks += 1
        if validated is None:
            prompt = build_pdf_icon_audit_prompt(
                page=page,
                block_id=target_block["id"],
                text=text,
                target_bbox=target_box,
                token_definitions=definitions,
            )
            active_provider = active_provider or create_pdf_icon_audit_provider()
            block_usage_before = provider_usage(active_provider)
            try:
                response = active_provider.inspect(prompt, crop)
                validated = validate_pdf_icon_audit_result(
                    response,
                    text=text,
                    token_definitions=definitions,
                )
            except PdfIconAuditError:
                append_ai_usage_event(
                    location.path,
                    stage="source_review",
                    operation="pdf_block_inspection",
                    status="failed",
                    usage=usage_delta(
                        block_usage_before,
                        provider_usage(active_provider),
                    ),
                    context={"block_id": target_block["id"], "page": page},
                )
                raise
            except Exception as error:
                append_ai_usage_event(
                    location.path,
                    stage="source_review",
                    operation="pdf_block_inspection",
                    status="failed",
                    usage=usage_delta(
                        block_usage_before,
                        provider_usage(active_provider),
                    ),
                    context={"block_id": target_block["id"], "page": page},
                )
                raise PdfIconAuditError(
                    f"Could not inspect icons in block {target_block['id']}."
                ) from error
            append_ai_usage_event(
                location.path,
                stage="source_review",
                operation="pdf_block_inspection",
                usage=usage_delta(
                    block_usage_before,
                    provider_usage(active_provider),
                ),
                context={
                    "block_id": target_block["id"],
                    "page": page,
                    "detected_icons": len(validated["icons"]),
                },
            )
            cache_blocks[target_block["id"]] = {
                "fingerprint": fingerprint,
                "prompt_version": PDF_ICON_AUDIT_PROMPT_VERSION,
                "provider": provider_name,
                "model": model_name,
                "audited_at": _utc_now(),
                "icons": validated["icons"],
                "summary": validated["summary"],
            }
            write_json_atomic(paths.pdf_icon_audit_state, cache)
        icons = validated["icons"]
        results.append(
            PdfIconAuditBlockResult(
                block_id=target_block["id"],
                group_id=target_block["group_id"],
                page=page,
                current_text=text,
                suggested_text=insert_icon_markers(text, icons),
                icons=icons,
                summary=validated["summary"],
                crop_url=f"/api/icon-crop?block={target_block['id']}",
                cached=cached,
            )
        )
    usage_after = provider_usage(active_provider) if active_provider else None
    usage = usage_delta(usage_before, usage_after)
    return PdfIconAuditResult(
        results=results,
        inspected_blocks=len(results),
        detected_icons=sum(len(result.icons) for result in results),
        cached_blocks=cached_blocks,
        usage=usage,
    )
