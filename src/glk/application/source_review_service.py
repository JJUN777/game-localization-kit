"""Prepare an editable source TXT and finalize human-reviewed corrections."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from glk.application.project_service import load_project
from glk.domain.source_block import SourceBlock, SourceBlockValidationError


SOURCE_REVIEW_FORMAT_VERSION = 1
_REVIEW_HEADER = f"[[GLK_REVIEW version={SOURCE_REVIEW_FORMAT_VERSION}]]"
_SEPARATOR = "======================"
_BLOCK_PATTERN = re.compile(r"^\[BLOCK ([a-z0-9][a-z0-9._-]*)\]$")
_TOKEN_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
_UNRESOLVED_ICON_PATTERN = re.compile(r"\[ICON:\s*[^\]]+\]", re.IGNORECASE)


class SourceReviewError(ValueError):
    """Raised when a review file cannot be prepared or finalized safely."""


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

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = self.ok
        return value


@dataclass(frozen=True, slots=True)
class ReviewFinalizeResult:
    project_path: str
    source_sha256: str
    total_blocks: int
    changed_blocks: int
    output_file: str | None
    approved_blocks_file: str | None
    token_changes_allowed: bool
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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _write_if_changed(path: Path, value: bytes) -> None:
    if path.is_file() and path.read_bytes() == value:
        return
    _write_bytes_atomic(path, value)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _load_source_blocks(project_path: Path) -> tuple[list[SourceBlock], bytes]:
    source_path = project_path / "segments/source.jsonl"
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


def _locator_line(block: SourceBlock) -> str:
    if block.source_type == "pdf":
        return f"[PAGE {block.page}]"
    return f"[SOURCE {block.source_file}]"


def render_source_review_text(blocks: list[SourceBlock]) -> bytes:
    """Render blocks as a human-editable TXT with protected location markers."""
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
    if not state_path.is_file():
        return "untracked"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "untracked"
    if (
        state.get("format_version") != SOURCE_REVIEW_FORMAT_VERSION
        or state.get("source_sha256") != source_sha256
    ):
        return "stale"
    return "current"


def prepare_project_source_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    dry_run: bool = False,
) -> ReviewPrepareResult:
    """Refresh draft/source.txt and create review/source.txt without overwriting it."""
    location = load_project(project, workspace_root)
    blocks, source_data = _load_source_blocks(location.path)
    source_sha256 = _sha256_bytes(source_data)
    rendered = render_source_review_text(blocks)
    draft_path = location.path / "draft/source.txt"
    review_path = location.path / "review/source.txt"
    state_path = location.path / "state/source_review.json"
    review_created = force or not review_path.exists()
    if review_created:
        review_status = "current"
    else:
        review_status = _review_status(state_path, source_sha256)

    if not dry_run:
        _write_if_changed(draft_path, rendered)
        (location.path / "final").mkdir(parents=True, exist_ok=True)
        if review_created:
            _write_bytes_atomic(review_path, rendered)
            state = {
                "schema_version": 1,
                "status": "prepared",
                "format_version": SOURCE_REVIEW_FORMAT_VERSION,
                "source_file": "segments/source.jsonl",
                "source_sha256": source_sha256,
                "total_blocks": len(blocks),
                "draft_file": "draft/source.txt",
                "review_file": "review/source.txt",
                "prepared_at": _utc_now(),
            }
            _write_json_atomic(state_path, state)

    return ReviewPrepareResult(
        project_path=str(location.path),
        source_sha256=source_sha256,
        total_blocks=len(blocks),
        draft_file=None if dry_run else str(draft_path),
        review_file=None if dry_run else str(review_path),
        review_created=review_created,
        review_status=review_status,
        dry_run=dry_run,
    )


def _parse_review_text(data: bytes, blocks: list[SourceBlock]) -> dict[str, str]:
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise SourceReviewError("Review TXT must be valid UTF-8.") from error
    if not lines or lines[0] != _REVIEW_HEADER:
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
        if expected.id in texts:
            raise SourceReviewError(f"Duplicate block in review TXT: {expected.id}")
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


def _load_allowed_tokens(project_path: Path) -> set[str]:
    prompt_path = project_path / "source/ocr_prompt.txt"
    if not prompt_path.is_file():
        return set()
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SourceReviewError(f"OCR prompt is not valid UTF-8: {prompt_path}") from error
    return set(_TOKEN_PATTERN.findall(prompt))


def _validate_reviewed_text(
    blocks: list[SourceBlock],
    texts: dict[str, str],
    *,
    allowed_tokens: set[str],
    allow_token_changes: bool,
) -> None:
    token_change_ids: list[str] = []
    for block in blocks:
        text = texts[block.id]
        if "�" in text:
            raise SourceReviewError(
                f"Block {block.id} still contains a Unicode replacement character."
            )
        if "[ILLEGIBLE]" in text.upper():
            raise SourceReviewError(f"Block {block.id} still contains [ILLEGIBLE].")
        if _UNRESOLVED_ICON_PATTERN.search(text):
            raise SourceReviewError(f"Block {block.id} still contains an unresolved icon.")

        tokens = _TOKEN_PATTERN.findall(text)
        token_stripped = _TOKEN_PATTERN.sub("", text)
        if "{" in token_stripped or "}" in token_stripped:
            raise SourceReviewError(f"Block {block.id} contains malformed token braces.")
        unknown_tokens = sorted(set(tokens) - allowed_tokens) if allowed_tokens else []
        if unknown_tokens:
            formatted = ", ".join(f"{{{token}}}" for token in unknown_tokens)
            raise SourceReviewError(
                f"Block {block.id} contains tokens not defined in the OCR prompt: {formatted}."
            )
        if Counter(tokens) != Counter(_TOKEN_PATTERN.findall(block.raw_text)):
            token_change_ids.append(block.id)

    if token_change_ids and not allow_token_changes:
        preview = ", ".join(token_change_ids[:5])
        suffix = "..." if len(token_change_ids) > 5 else ""
        raise SourceReviewError(
            "Icon token changes require explicit confirmation with "
            f"--allow-token-changes. Changed blocks: {preview}{suffix}"
        )


def _serialize_blocks(blocks: list[SourceBlock]) -> bytes:
    return (
        "".join(
            json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for block in blocks
        )
    ).encode("utf-8")


def finalize_project_source_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    allow_token_changes: bool = False,
    dry_run: bool = False,
) -> ReviewFinalizeResult:
    """Validate the edited review TXT and produce approved TXT/JSONL outputs."""
    location = load_project(project, workspace_root)
    blocks, source_data = _load_source_blocks(location.path)
    source_sha256 = _sha256_bytes(source_data)
    review_path = location.path / "review/source.txt"
    state_path = location.path / "state/source_review.json"
    if not review_path.is_file():
        raise SourceReviewError(
            f"Review TXT not found: {review_path}. Run glk review prepare first."
        )
    if _review_status(state_path, source_sha256) != "current":
        raise SourceReviewError(
            "Review TXT is stale or has no matching source state. "
            "Compare your edits, then run glk review prepare --force to reset it."
        )

    texts = _parse_review_text(review_path.read_bytes(), blocks)
    _validate_reviewed_text(
        blocks,
        texts,
        allowed_tokens=_load_allowed_tokens(location.path),
        allow_token_changes=allow_token_changes,
    )
    approved_blocks: list[SourceBlock] = []
    changed_blocks = 0
    for block in blocks:
        corrected = texts[block.id]
        changed = corrected != block.raw_text
        changed_blocks += int(changed)
        approved = replace(
            block,
            corrected_text=corrected if changed else None,
            status="approved",
        )
        approved.validate()
        approved_blocks.append(approved)

    final_text = render_source_review_text(approved_blocks)
    approved_data = _serialize_blocks(approved_blocks)
    final_path = location.path / "final/source.txt"
    approved_path = location.path / "segments/approved_source.jsonl"
    if not dry_run:
        _write_bytes_atomic(final_path, final_text)
        _write_bytes_atomic(approved_path, approved_data)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "status": "approved",
                "changed_blocks": changed_blocks,
                "review_sha256": _sha256_bytes(review_path.read_bytes()),
                "final_file": "final/source.txt",
                "final_sha256": _sha256_bytes(final_text),
                "approved_blocks_file": "segments/approved_source.jsonl",
                "approved_blocks_sha256": _sha256_bytes(approved_data),
                "approved_at": _utc_now(),
            }
        )
        _write_json_atomic(state_path, state)

    return ReviewFinalizeResult(
        project_path=str(location.path),
        source_sha256=source_sha256,
        total_blocks=len(blocks),
        changed_blocks=changed_blocks,
        output_file=None if dry_run else str(final_path),
        approved_blocks_file=None if dry_run else str(approved_path),
        token_changes_allowed=allow_token_changes,
        dry_run=dry_run,
    )
