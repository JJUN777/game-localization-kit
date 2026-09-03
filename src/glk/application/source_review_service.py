"""Prepare, edit, inspect, and finalize human-reviewed source text."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
import uuid

from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application.project_service import ProjectLocation, load_project
from glk.application.review_types import (
    SourceReviewBlock,
    SourceReviewDocument,
    SourceReviewGroup,
)
from glk.domain.source_block import (
    SOURCE_BLOCK_SCHEMA_VERSION,
    SourceBlock,
    SourceBlockValidationError,
)
from glk.domain.workspace import WorkspacePaths, is_pdf_source_file
from glk.extraction.layout import LAYOUT_RECOVERY_WARNING_PREFIX


SOURCE_REVIEW_FORMAT_VERSION = 2
_REVIEW_HEADER = f"[[GLK_REVIEW version={SOURCE_REVIEW_FORMAT_VERSION}]]"
_SUPPORTED_HEADERS = {"[[GLK_REVIEW version=1]]", _REVIEW_HEADER}
_SEPARATOR = "======================"
_BLOCK_PATTERN = re.compile(r"^\[BLOCK ([a-z0-9][a-z0-9._-]*)\]$")
_TOKEN_PATTERN = re.compile(r"\[(?!(?:ICON|ILLEGIBLE)\])([A-Z][A-Z0-9_]*)\]")
_TOKEN_DEFINITION_PATTERN = re.compile(
    r"^\s*-\s*\[([A-Z][A-Z0-9_]*)\]\s*:\s*\S.*$",
    re.MULTILINE,
)
_MALFORMED_TOKEN_PATTERN = re.compile(
    r"\[(?!(?:ICON\s*:|ILLEGIBLE(?:\]|$)))[A-Z][A-Z0-9_]*(?:[^A-Z0-9_\]]|$)",
)
_UNRESOLVED_ICON_PATTERN = re.compile(r"\[ICON:\s*[^\]]+\]", re.IGNORECASE)


class SourceReviewError(ValueError):
    """Raised when a review file cannot be prepared, edited, or finalized safely."""

    code = "INVALID_REQUEST"


class SourceReviewConflictError(SourceReviewError):
    """Raised when source review state changed after it was loaded."""

    code = "REVIEW_CONFLICT"


class SourceReviewUnresolvedTextError(SourceReviewError):
    """Raised when reviewed text still contains an unresolved OCR marker."""

    code = "SOURCE_REVIEW_UNRESOLVED_TEXT"


class SourceReviewTokenError(SourceReviewError):
    """Raised when reviewed text contains an invalid or unknown icon token."""

    code = "SOURCE_REVIEW_TOKEN_INVALID"


class SourceReviewTokenConfirmationError(SourceReviewError):
    """Raised when icon-token edits have not been explicitly confirmed."""

    code = "SOURCE_REVIEW_TOKEN_CONFIRMATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class ReviewPrepareResult:
    project_path: str
    source_sha256: str
    total_blocks: int
    draft_file: str | None
    review_file: str | None
    review_created: bool
    review_status: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReviewFinalizeResult:
    project_path: str
    source_sha256: str
    total_blocks: int
    changed_blocks: int
    output_file: str | None
    approved_blocks_file: str | None
    token_changes_allowed: bool
    unresolved_icons_allowed: bool = False
    unresolved_icon_blocks: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_hash(text: str) -> str:
    return "sha256:" + _sha256_bytes(text.encode("utf-8"))


def _write_if_changed(path: Path, value: bytes) -> None:
    if path.is_file() and path.read_bytes() == value:
        return
    _write_bytes_atomic(path, value)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceReviewError(f"Invalid source review state: {path}") from error
    if not isinstance(value, dict):
        raise SourceReviewError(f"Source review state must be a JSON object: {path}")
    return value


def _load_source_blocks(project_path: Path) -> tuple[list[SourceBlock], bytes]:
    source_path = WorkspacePaths(project_path).source_segments
    if not source_path.is_file():
        raise SourceReviewError(
            f"Review-source blocks not found: {source_path}. Run glk segment first."
        )
    data = source_path.read_bytes()
    blocks: list[SourceBlock] = []
    line_number = 0
    try:
        for line_number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
            if line.strip():
                blocks.append(SourceBlock.from_dict(json.loads(line)))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceBlockValidationError,
        TypeError,
    ) as error:
        raise SourceReviewError(
            f"Invalid source block JSONL at line {line_number}: {error}"
        ) from error
    if not blocks:
        raise SourceReviewError("Review-source block JSONL is empty.")
    if len({block.id for block in blocks}) != len(blocks):
        raise SourceReviewError("Review-source block JSONL contains duplicate block IDs.")
    return blocks, data


def _locator_key(block: SourceBlock) -> tuple[str, str | int]:
    return (
        ("pdf", int(block.page or 0))
        if block.source_type == "pdf"
        else ("image", block.source_file)
    )


def _locator_line(block: SourceBlock) -> str:
    if block.source_type == "pdf":
        return f"[PAGE {block.page}]"
    return f"[SOURCE {block.source_file}]"


def render_source_review_text(blocks: list[SourceBlock]) -> bytes:
    """Render included blocks in their current human-approved reading order."""
    lines = [_REVIEW_HEADER, ""]
    for block in blocks:
        text = block.effective_text.strip()
        if not text:
            raise SourceReviewError(f"Block {block.id} has empty source text.")
        end_marker = f"[[GLK_END {block.id}]]"
        if end_marker in text.splitlines():
            raise SourceReviewError(
                f"Block {block.id} contains a reserved review marker line."
            )
        lines.extend(
            (
                _locator_line(block),
                f"[BLOCK {block.id}]",
                text,
                end_marker,
                "",
                _SEPARATOR,
                "",
            )
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _review_status(state_path: Path, source_sha256: str) -> str:
    try:
        state = _read_state(state_path)
    except SourceReviewError:
        return "untracked"
    if not state:
        return "untracked"
    if (
        state.get("format_version") not in {1, SOURCE_REVIEW_FORMAT_VERSION}
        or state.get("source_sha256") != source_sha256
    ):
        return "stale"
    return "current"


def _manual_blocks_from_state(state: dict[str, Any]) -> dict[str, SourceBlock]:
    values = state.get("manual_blocks", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        raise SourceReviewError("source_review manual_blocks must be a list.")
    blocks: dict[str, SourceBlock] = {}
    for value in values:
        try:
            block = SourceBlock.from_dict(value)
        except (SourceBlockValidationError, TypeError) as error:
            raise SourceReviewError(f"Invalid manual review block: {error}") from error
        if not block.id.startswith("manual-") or block.id in blocks:
            raise SourceReviewError(f"Invalid or duplicate manual block ID: {block.id}")
        blocks[block.id] = block
    return blocks


def _layout(
    originals: list[SourceBlock],
    state: dict[str, Any],
) -> tuple[list[str], set[str], dict[str, SourceBlock]]:
    original_ids = [block.id for block in originals]
    manual = _manual_blocks_from_state(state)
    ordered_value = state.get("ordered_block_ids")
    excluded_value = state.get("excluded_block_ids", [])
    if ordered_value is None:
        ordered = original_ids + list(manual)
    elif isinstance(ordered_value, list) and all(
        isinstance(value, str) for value in ordered_value
    ):
        ordered = list(ordered_value)
    else:
        raise SourceReviewError("source_review ordered_block_ids must be a string list.")
    if not isinstance(excluded_value, list) or not all(
        isinstance(value, str) for value in excluded_value
    ):
        raise SourceReviewError("source_review excluded_block_ids must be a string list.")
    excluded = set(excluded_value)
    known = set(original_ids) | set(manual)
    if len(ordered) != len(set(ordered)) or set(ordered) != known:
        raise SourceReviewError(
            "Source review layout must contain every extracted and manual block exactly once."
        )
    if not excluded <= set(original_ids):
        raise SourceReviewError("Only extracted blocks can be excluded.")
    return ordered, excluded, manual


def _parse_review_text(data: bytes, blocks: list[SourceBlock]) -> dict[str, str]:
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise SourceReviewError("Review TXT must be valid UTF-8.") from error
    if not lines or lines[0] not in _SUPPORTED_HEADERS:
        raise SourceReviewError(
            "Review TXT header is missing or unsupported. Run glk review prepare --force."
        )

    texts: dict[str, str] = {}
    index = 1
    for expected in blocks:
        while index < len(lines) and lines[index] in {"", _SEPARATOR}:
            index += 1
        expected_locator = _locator_line(expected)
        if index >= len(lines) or lines[index] != expected_locator:
            found = lines[index] if index < len(lines) else "end of file"
            raise SourceReviewError(
                f"Location marker for block {expected.id} was changed or removed; "
                f"expected {expected_locator!r}, found {found!r}."
            )
        index += 1
        expected_block = f"[BLOCK {expected.id}]"
        if index >= len(lines) or lines[index] != expected_block:
            found = lines[index] if index < len(lines) else "end of file"
            match = _BLOCK_PATTERN.fullmatch(found) if isinstance(found, str) else None
            detail = f"found block {match.group(1)}" if match else f"found {found!r}"
            raise SourceReviewError(
                f"Block marker order is invalid; expected {expected.id}, {detail}."
            )
        index += 1
        end_marker = f"[[GLK_END {expected.id}]]"
        try:
            end_index = lines.index(end_marker, index)
        except ValueError as error:
            raise SourceReviewError(
                f"End marker for block {expected.id} was changed or removed."
            ) from error
        body = "\n".join(lines[index:end_index]).strip()
        if not body:
            raise SourceReviewError(f"Reviewed text for block {expected.id} is empty.")
        texts[expected.id] = body
        index = end_index + 1

    while index < len(lines) and lines[index] in {"", _SEPARATOR}:
        index += 1
    if index != len(lines):
        match = _BLOCK_PATTERN.fullmatch(lines[index])
        if match:
            raise SourceReviewError(f"Unknown or duplicate block in review TXT: {match.group(1)}")
        raise SourceReviewError(f"Unexpected content after the final block: {lines[index]!r}")
    return texts


def _review_context(
    project_path: Path,
) -> tuple[
    list[SourceBlock],
    bytes,
    dict[str, Any],
    list[str],
    set[str],
    dict[str, SourceBlock],
    dict[str, str],
]:
    paths = WorkspacePaths(project_path)
    originals, source_data = _load_source_blocks(project_path)
    source_sha256 = _sha256_bytes(source_data)
    state = _read_state(paths.source_review_state)
    if _review_status(paths.source_review_state, source_sha256) != "current":
        raise SourceReviewConflictError(
            "Review TXT is stale or has no matching source state. "
            "Compare your edits, then run glk review prepare --force to reset it."
        )
    if not paths.source_review.is_file():
        raise SourceReviewError(
            f"Review TXT not found: {paths.source_review}. Run glk review prepare first."
        )
    ordered, excluded, manual = _layout(originals, state)
    all_blocks = {block.id: block for block in originals}
    all_blocks.update(manual)
    included = [all_blocks[block_id] for block_id in ordered if block_id not in excluded]
    texts = _parse_review_text(paths.source_review.read_bytes(), included)
    return originals, source_data, state, ordered, excluded, manual, texts


def prepare_project_source_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    dry_run: bool = False,
) -> ReviewPrepareResult:
    """Refresh source draft and create review TXT without overwriting human work."""
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    blocks, source_data = _load_source_blocks(location.path)
    source_sha256 = _sha256_bytes(source_data)
    rendered = render_source_review_text(blocks)
    review_created = force or not paths.source_review.exists()
    review_status = (
        "current"
        if review_created
        else _review_status(paths.source_review_state, source_sha256)
    )

    if not dry_run:
        _write_if_changed(paths.source_draft, rendered)
        if review_created:
            _write_bytes_atomic(paths.source_review, rendered)
            _write_json_atomic(
                paths.source_review_state,
                {
                    "schema_version": 1,
                    "status": "prepared",
                    "format_version": SOURCE_REVIEW_FORMAT_VERSION,
                    "source_file": paths.relative(paths.source_segments),
                    "source_sha256": source_sha256,
                    "total_blocks": len(blocks),
                    "draft_file": paths.relative(paths.source_draft),
                    "review_file": paths.relative(paths.source_review),
                    "ordered_block_ids": [block.id for block in blocks],
                    "excluded_block_ids": [],
                    "manual_blocks": [],
                    "prepared_at": _utc_now(),
                },
            )

    return ReviewPrepareResult(
        project_path=str(location.path),
        source_sha256=source_sha256,
        total_blocks=len(blocks),
        draft_file=None if dry_run else str(paths.source_draft),
        review_file=None if dry_run else str(paths.source_review),
        review_created=review_created,
        review_status=review_status,
        dry_run=dry_run,
    )


def _load_allowed_tokens(project_path: Path) -> set[str]:
    prompt_path = WorkspacePaths(project_path).input_ocr_prompt
    if not prompt_path.is_file():
        return set()
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SourceReviewError(f"OCR prompt is not valid UTF-8: {prompt_path}") from error
    return set(_TOKEN_DEFINITION_PATTERN.findall(prompt))


def _validate_reviewed_text(
    blocks: list[SourceBlock],
    texts: dict[str, str],
    *,
    allowed_tokens: set[str],
    allow_token_changes: bool,
    allow_unresolved_icons: bool,
) -> None:
    token_change_ids: list[str] = []
    for block in blocks:
        text = texts[block.id]
        if "�" in text:
            raise SourceReviewUnresolvedTextError(
                f"Block {block.id} still contains a Unicode replacement character."
            )
        if "[ILLEGIBLE]" in text.upper():
            raise SourceReviewUnresolvedTextError(
                f"Block {block.id} still contains [ILLEGIBLE]."
            )
        if not allow_unresolved_icons and _UNRESOLVED_ICON_PATTERN.search(text):
            raise SourceReviewUnresolvedTextError(
                f"Block {block.id} still contains an unresolved icon."
            )
        tokens = _TOKEN_PATTERN.findall(text)
        if _MALFORMED_TOKEN_PATTERN.search(text):
            raise SourceReviewTokenError(
                f"Block {block.id} contains a malformed square-bracket icon token."
            )
        unknown_tokens = sorted(set(tokens) - allowed_tokens) if allowed_tokens else []
        if unknown_tokens:
            formatted = ", ".join(f"[{token}]" for token in unknown_tokens)
            raise SourceReviewTokenError(
                f"Block {block.id} contains tokens not defined in the OCR prompt: {formatted}."
            )
        if not block.id.startswith("manual-") and Counter(tokens) != Counter(
            _TOKEN_PATTERN.findall(block.raw_text)
        ):
            token_change_ids.append(block.id)
    if token_change_ids and not allow_token_changes:
        preview = ", ".join(token_change_ids[:5])
        suffix = "..." if len(token_change_ids) > 5 else ""
        raise SourceReviewTokenConfirmationError(
            "Icon token changes require explicit confirmation. "
            f"Changed blocks: {preview}{suffix}"
        )


def _serialize_blocks(blocks: list[SourceBlock]) -> bytes:
    return "".join(
        json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for block in blocks
    ).encode("utf-8")


def _group_order(originals: list[SourceBlock]) -> dict[tuple[str, str | int], int]:
    result: dict[tuple[str, str | int], int] = {}
    for block in originals:
        result.setdefault(_locator_key(block), len(result))
    return result


def _manual_id(source_type: str, page: int | None, source_file: str) -> str:
    prefix = f"p{int(page or 0):04d}" if source_type == "pdf" else "image"
    return f"manual-{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class _SourceReviewSaveContext:
    location: ProjectLocation
    paths: WorkspacePaths
    originals: tuple[SourceBlock, ...]
    source_sha256: str
    state: dict[str, Any]
    prior_manual: dict[str, SourceBlock]


@dataclass(frozen=True, slots=True)
class _SubmittedReviewBlock:
    block: SourceBlock
    text: str
    excluded: bool
    manual: bool


@dataclass(frozen=True, slots=True)
class _NormalizedSourceReview:
    ordered_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    manual_blocks: dict[str, SourceBlock]
    rendered_blocks: tuple[SourceBlock, ...]


def _load_source_review_save_context(
    *,
    project: str | Path,
    workspace_root: str | Path,
    expected_review_sha256: str,
) -> _SourceReviewSaveContext:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    originals, source_data = _load_source_blocks(location.path)
    source_sha256 = _sha256_bytes(source_data)
    if _review_status(paths.source_review_state, source_sha256) != "current":
        raise SourceReviewConflictError(
            "Source review is stale; refresh it before saving."
        )
    if not paths.source_review.is_file():
        raise SourceReviewError("Source review TXT does not exist.")
    current_hash = _sha256_bytes(paths.source_review.read_bytes())
    if current_hash != expected_review_sha256:
        raise SourceReviewConflictError(
            "Source review changed after this browser loaded it. Reload before saving."
        )
    state = _read_state(paths.source_review_state)
    _, _, prior_manual = _layout(originals, state)
    return _SourceReviewSaveContext(
        location,
        paths,
        tuple(originals),
        source_sha256,
        state,
        prior_manual,
    )


def _build_manual_review_block(
    value: dict[str, Any],
    *,
    text: str,
    prior_manual: dict[str, SourceBlock],
) -> SourceBlock:
    source_type = value.get("source_type")
    source_file = value.get("source_file")
    page = value.get("page")
    bbox = value.get("bbox")
    if source_type not in {"pdf", "image"} or not isinstance(source_file, str):
        raise SourceReviewError("Manual blocks need a valid source type and file.")
    if source_type == "pdf" and (
        not isinstance(page, int) or isinstance(page, bool) or page <= 0
    ):
        raise SourceReviewError("Manual PDF blocks need a positive page.")
    if source_type == "image":
        page = None
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in bbox
        )
    ):
        raise SourceReviewError("Manual blocks require a four-number bbox.")
    coordinates = (
        float(bbox[0]),
        float(bbox[1]),
        float(bbox[2]),
        float(bbox[3]),
    )
    if (
        coordinates[0] >= coordinates[2]
        or coordinates[1] >= coordinates[3]
    ):
        raise SourceReviewError("Manual block bbox must have a positive area.")

    submitted_id = value.get("id")
    candidate_id = submitted_id if isinstance(submitted_id, str) else ""
    if candidate_id in prior_manual:
        raw_text = prior_manual[candidate_id].raw_text
        candidate_id = prior_manual[candidate_id].id
    else:
        candidate_id = _manual_id(source_type, page, source_file)
        raw_text = text.strip()
    try:
        block = SourceBlock(
            schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
            id=candidate_id,
            source_type=source_type,
            source_file=source_file,
            page=page,
            source_order=1,
            block_order=1,
            block_type="paragraph",
            raw_text=raw_text,
            corrected_text=None,
            bbox=coordinates,
            legibility="clear",
            status="corrected",
            warnings=(),
            source_refs=("manual-review",),
            source_hash=_source_hash(raw_text),
        )
        block.validate()
    except SourceBlockValidationError as error:
        raise SourceReviewError(f"Invalid manual block: {error}") from error
    return block


def _parse_submitted_review_block(
    value: Any,
    *,
    original_by_id: dict[str, SourceBlock],
    prior_manual: dict[str, SourceBlock],
) -> _SubmittedReviewBlock | None:
    if not isinstance(value, dict):
        raise SourceReviewError("Every browser review block must be an object.")
    block_id = value.get("id")
    text = value.get("text")
    excluded = value.get("excluded", False)
    if not isinstance(excluded, bool):
        raise SourceReviewError("excluded must be true or false.")
    if not isinstance(text, str):
        raise SourceReviewError("Every browser review block needs text.")
    if isinstance(block_id, str) and block_id in original_by_id:
        return _SubmittedReviewBlock(
            original_by_id[block_id],
            text,
            excluded,
            False,
        )
    if excluded:
        return None
    return _SubmittedReviewBlock(
        _build_manual_review_block(
            value,
            text=text,
            prior_manual=prior_manual,
        ),
        text,
        False,
        True,
    )


def _normalize_source_review_blocks(
    blocks: list[dict[str, Any]],
    context: _SourceReviewSaveContext,
) -> _NormalizedSourceReview:
    if not isinstance(blocks, list) or not blocks:
        raise SourceReviewError("blocks must be a non-empty list.")
    originals = list(context.originals)
    original_by_id = {block.id: block for block in originals}
    group_order = _group_order(originals)
    group_sources = {
        _locator_key(block): block.source_file for block in originals
    }
    seen_originals: set[str] = set()
    seen_ids: set[str] = set()
    ordered_ids: list[str] = []
    excluded_ids: list[str] = []
    manual_blocks: dict[str, SourceBlock] = {}
    rendered_blocks: list[SourceBlock] = []
    previous_group = -1

    for value in blocks:
        submitted = _parse_submitted_review_block(
            value,
            original_by_id=original_by_id,
            prior_manual=context.prior_manual,
        )
        if submitted is None:
            continue
        block = submitted.block
        block_id = block.id
        if submitted.manual:
            manual_blocks[block_id] = block
        else:
            seen_originals.add(block_id)
        if block_id in seen_ids:
            raise SourceReviewError(
                f"Duplicate browser review block: {block_id}"
            )
        seen_ids.add(block_id)

        group = _locator_key(block)
        if group not in group_order:
            raise SourceReviewError(
                "Blocks cannot be moved to an unknown page or image."
            )
        if submitted.manual and block.source_file != group_sources[group]:
            raise SourceReviewError(
                "Manual blocks must use the source file shown for their page or image."
            )
        current_group = group_order[group]
        if current_group < previous_group:
            raise SourceReviewError(
                "Pages and image files cannot be reordered; "
                "only blocks within them can move."
            )
        previous_group = current_group
        ordered_ids.append(block_id)
        if submitted.excluded:
            excluded_ids.append(block_id)
            continue
        clean_text = submitted.text.strip()
        if not clean_text:
            raise SourceReviewError(
                f"Block {block_id} has empty reviewed text."
            )
        rendered_blocks.append(
            replace(
                block,
                corrected_text=(
                    clean_text if clean_text != block.raw_text else None
                ),
            )
        )

    if seen_originals != set(original_by_id):
        missing = sorted(set(original_by_id) - seen_originals)
        raise SourceReviewError(
            "Every extracted block must be retained or explicitly excluded. "
            f"Missing: {', '.join(missing[:5])}"
        )
    if not rendered_blocks:
        raise SourceReviewError("At least one reviewed source block must remain.")
    return _NormalizedSourceReview(
        tuple(ordered_ids),
        tuple(excluded_ids),
        manual_blocks,
        tuple(rendered_blocks),
    )


def _write_source_review_save(
    *,
    context: _SourceReviewSaveContext,
    normalized: _NormalizedSourceReview,
) -> None:
    rendered = render_source_review_text(list(normalized.rendered_blocks))
    _write_bytes_atomic(context.paths.source_review, rendered)
    state = dict(context.state)
    state.update(
        {
            "status": "prepared",
            "format_version": SOURCE_REVIEW_FORMAT_VERSION,
            "source_sha256": context.source_sha256,
            "total_blocks": len(context.originals),
            "ordered_block_ids": list(normalized.ordered_ids),
            "excluded_block_ids": list(normalized.excluded_ids),
            "manual_blocks": [
                normalized.manual_blocks[block_id].to_dict()
                for block_id in normalized.ordered_ids
                if block_id in normalized.manual_blocks
            ],
            "review_sha256": _sha256_bytes(rendered),
            "updated_at": _utc_now(),
        }
    )
    for key in (
        "final_sha256",
        "approved_blocks_sha256",
        "approved_at",
        "final_file",
        "approved_blocks_file",
    ):
        state.pop(key, None)
    _write_json_atomic(context.paths.source_review_state, state)


def save_project_source_review(
    *,
    project: str | Path,
    blocks: list[dict[str, Any]],
    expected_review_sha256: str,
    workspace_root: str | Path = "workspaces",
) -> SourceReviewDocument:
    """Save browser edits with optimistic locking and explicit layout decisions."""
    context = _load_source_review_save_context(
        project=project,
        workspace_root=workspace_root,
        expected_review_sha256=expected_review_sha256,
    )
    normalized = _normalize_source_review_blocks(blocks, context)
    _write_source_review_save(context=context, normalized=normalized)
    return get_project_source_review_document(
        project=context.location.path,
        workspace_root=workspace_root,
    )


def _qa_issues(project_path: Path) -> dict[str, list[dict[str, Any]]]:
    path = WorkspacePaths(project_path).source_qa_json
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    issues = value.get("issues", []) if isinstance(value, dict) else []
    result: dict[str, list[dict[str, Any]]] = {}
    if isinstance(issues, list):
        for issue in issues:
            if (
                isinstance(issue, dict)
                and isinstance(issue.get("block_id"), str)
                and issue.get("code") != "SOURCE_WARNING"
            ):
                result.setdefault(issue["block_id"], []).append(issue)
    return result


def get_project_source_review_document(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
) -> SourceReviewDocument:
    """Return the browser-facing source review document."""
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    originals, _, state, ordered, excluded, manual, texts = _review_context(
        location.path
    )
    all_blocks = {block.id: block for block in originals}
    all_blocks.update(manual)
    issues = _qa_issues(location.path)
    groups: list[SourceReviewGroup] = []
    group_ids: dict[tuple[str, str | int], str] = {}
    group_indexes: dict[tuple[str, str | int], int] = {}
    document_blocks: list[SourceReviewBlock] = []
    for block_id in ordered:
        block = all_blocks[block_id]
        key = _locator_key(block)
        if key not in group_ids:
            group_id = f"group-{len(groups) + 1}"
            group_ids[key] = group_id
            group_indexes[key] = len(groups)
            groups.append(
                {
                    "id": group_id,
                    "source_type": block.source_type,
                    "page": block.page,
                    "source_file": block.source_file,
                    "label": (
                        f"PAGE {block.page}"
                        if block.source_type == "pdf"
                        else block.source_file.removeprefix("01_input/images/")
                    ),
                    "image_url": f"/api/source-image?group={group_id}",
                    "layout_warnings": 0,
                }
            )
        is_excluded = block_id in excluded
        text = block.effective_text if is_excluded else texts[block_id]
        layout_warning_count = sum(
            warning.startswith(LAYOUT_RECOVERY_WARNING_PREFIX)
            for warning in block.warnings
        )
        groups[group_indexes[key]]["layout_warnings"] += layout_warning_count
        document_blocks.append(
            {
                "id": block.id,
                "group_id": group_ids[key],
                "source_type": block.source_type,
                "source_file": block.source_file,
                "page": block.page,
                "block_type": block.block_type,
                "text": text,
                "raw_text": block.raw_text,
                "bbox": list(block.bbox) if block.bbox is not None else None,
                "manual": block.id.startswith("manual-"),
                "excluded": is_excluded,
                "changed": text != block.raw_text,
                "warnings": list(block.warnings),
                "layout_warnings": layout_warning_count,
                "issues": issues.get(block.id, []),
            }
        )
    review_data = paths.source_review.read_bytes()
    source_type = "pdf" if is_pdf_source_file(location.manifest.source_file) else "image"
    return {
        "ok": True,
        "project_id": location.manifest.project_id,
        "project_name": location.manifest.name,
        "source_type": source_type,
        "review_status": state.get("status", "prepared"),
        "review_sha256": _sha256_bytes(review_data),
        "source_sha256": state.get("source_sha256"),
        "groups": groups,
        "blocks": document_blocks,
        "summary": {
            "blocks": len(document_blocks),
            "included": sum(not block["excluded"] for block in document_blocks),
            "excluded": sum(block["excluded"] for block in document_blocks),
            "manual": sum(block["manual"] for block in document_blocks),
            "changed": sum(block["changed"] for block in document_blocks),
            "warnings": sum(len(block["warnings"]) for block in document_blocks),
            "layout_warnings": sum(
                block["layout_warnings"] for block in document_blocks
            ),
            "issues": sum(len(block["issues"]) for block in document_blocks),
        },
        "original_pdf_url": "/api/original-pdf" if source_type == "pdf" else None,
    }


def _approved_blocks(
    project_path: Path,
    originals: list[SourceBlock],
    state: dict[str, Any],
    ordered: list[str],
    excluded: set[str],
    manual: dict[str, SourceBlock],
    texts: dict[str, str],
) -> tuple[list[SourceBlock], int]:
    all_blocks = {block.id: block for block in originals}
    all_blocks.update(manual)
    included = [all_blocks[block_id] for block_id in ordered if block_id not in excluded]
    _validate_reviewed_text(
        included,
        texts,
        allowed_tokens=_load_allowed_tokens(project_path),
        allow_token_changes=bool(state.get("_allow_token_changes")),
        allow_unresolved_icons=bool(state.get("_allow_unresolved_icons")),
    )
    approved: list[SourceBlock] = []
    changed_blocks = 0
    per_group: dict[tuple[str, str | int], int] = {}
    for source_order, block in enumerate(included, start=1):
        group = _locator_key(block)
        per_group[group] = per_group.get(group, 0) + 1
        corrected = texts[block.id]
        changed = corrected != block.raw_text
        changed_blocks += int(changed or block.id.startswith("manual-"))
        value = replace(
            block,
            source_order=source_order,
            block_order=per_group[group],
            corrected_text=corrected if changed else None,
            status="approved",
        )
        value.validate()
        approved.append(value)
    return approved, changed_blocks


def finalize_project_source_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    allow_token_changes: bool = False,
    allow_unresolved_icons: bool = False,
    dry_run: bool = False,
) -> ReviewFinalizeResult:
    """Validate the current layout and produce approved source outputs."""
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    originals, source_data, state, ordered, excluded, manual, texts = _review_context(
        location.path
    )
    validation_state = dict(state)
    validation_state["_allow_token_changes"] = allow_token_changes
    validation_state["_allow_unresolved_icons"] = allow_unresolved_icons
    approved_blocks, changed_blocks = _approved_blocks(
        location.path,
        originals,
        validation_state,
        ordered,
        excluded,
        manual,
        texts,
    )
    final_text = render_source_review_text(approved_blocks)
    approved_data = _serialize_blocks(approved_blocks)
    unresolved_icon_ids = [
        block.id
        for block in approved_blocks
        if _UNRESOLVED_ICON_PATTERN.search(block.effective_text)
    ]
    if not dry_run:
        _write_bytes_atomic(paths.source_final, final_text)
        _write_bytes_atomic(paths.approved_source_segments, approved_data)
        state.update(
            {
                "status": "approved",
                "changed_blocks": changed_blocks,
                "review_sha256": _sha256_bytes(paths.source_review.read_bytes()),
                "final_file": paths.relative(paths.source_final),
                "final_sha256": _sha256_bytes(final_text),
                "approved_blocks_file": paths.relative(
                    paths.approved_source_segments
                ),
                "approved_blocks_sha256": _sha256_bytes(approved_data),
                "unresolved_icons_allowed": allow_unresolved_icons,
                "unresolved_icon_block_ids": unresolved_icon_ids,
                "approved_at": _utc_now(),
            }
        )
        _write_json_atomic(paths.source_review_state, state)
    return ReviewFinalizeResult(
        project_path=str(location.path),
        source_sha256=_sha256_bytes(source_data),
        total_blocks=len(approved_blocks),
        changed_blocks=changed_blocks,
        output_file=None if dry_run else str(paths.source_final),
        approved_blocks_file=(
            None if dry_run else str(paths.approved_source_segments)
        ),
        token_changes_allowed=allow_token_changes,
        unresolved_icons_allowed=allow_unresolved_icons,
        unresolved_icon_blocks=len(unresolved_icon_ids),
        dry_run=dry_run,
    )
