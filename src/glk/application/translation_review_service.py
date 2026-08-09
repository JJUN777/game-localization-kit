"""Prepare, inspect, and finalize human-reviewed translations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application.project_service import inspect_project, load_project
from glk.application.review_types import (
    TranslationReviewBlock,
    TranslationReviewDocument,
    TranslationReviewIssuePayload,
    TranslationReviewTerm,
)
from glk.domain.approved_translation import (
    APPROVED_TRANSLATION_SCHEMA_VERSION,
    ApprovedTranslationSegment,
)
from glk.domain.translation_qa import check_translation_contract
from glk.domain.translation_segment import (
    TranslationSegment,
    TranslationSegmentValidationError,
)
from glk.domain.workspace import IMAGE_SOURCE_ROOT, WorkspacePaths, is_pdf_source_file


TRANSLATION_REVIEW_VERSION = "translation-review-v2"
TRANSLATION_REVIEW_FORMAT_VERSION = 1
_REVIEW_HEADER = (
    f"[[GLK_TRANSLATION_REVIEW version={TRANSLATION_REVIEW_FORMAT_VERSION}]]"
)
_SEPARATOR = "======================"
_FINAL_SECTION_SEPARATOR = "----------------------"
_BLOCK_PATTERN = re.compile(r"^\[BLOCK ([a-z0-9][a-z0-9._-]*)\]$")
_HANGUL_PATTERN = re.compile(r"[가-힣]")
_LATIN_PATTERN = re.compile(r"[A-Za-z]")
_OVERRIDABLE_QA_ERROR_CODES = {
    "number_changed",
    "approved_term_missing",
    "keep_term_changed",
}
_MAX_QA_OVERRIDE_REASON_LENGTH = 1000


class TranslationReviewError(ValueError):
    """Raised when a translation review cannot be processed safely."""

    code = "INVALID_REQUEST"


class TranslationReviewConflictError(TranslationReviewError):
    """Raised when optimistic review locking detects a concurrent change."""

    code = "REVIEW_CONFLICT"


class TranslationReviewBlockMismatchError(TranslationReviewError):
    """Raised when submitted block IDs do not match the active review."""

    code = "TRANSLATION_REVIEW_BLOCK_MISMATCH"


class TranslationReviewParseError(TranslationReviewError):
    def __init__(self, code: str, message: str, block_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.block_id = block_id


@dataclass(frozen=True, slots=True)
class TranslationReviewIssue:
    severity: str
    code: str
    block_id: str | None
    message: str

    def to_dict(self) -> TranslationReviewIssuePayload:
        return {
            "severity": self.severity,
            "code": self.code,
            "block_id": self.block_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TranslationReviewPrepareResult:
    project_path: str
    total_blocks: int
    draft_sha256: str
    review_file: str | None
    review_created: bool
    review_status: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranslationQaResult:
    project_path: str
    total_blocks: int
    error_count: int
    warning_count: int
    info_count: int
    issues: tuple[TranslationReviewIssue, ...]
    json_report: str | None
    markdown_report: str | None
    dry_run: bool = False

    @property
    def passed(self) -> bool:
        return self.error_count == 0

    @property
    def ok(self) -> bool:
        return self.passed

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        value["passed"] = self.passed
        value["ok"] = self.ok
        return value


@dataclass(frozen=True, slots=True)
class TranslationFinalizeResult:
    project_path: str
    total_blocks: int
    changed_blocks: int
    error_count: int
    warning_count: int
    issues: tuple[TranslationReviewIssue, ...]
    output_file: str | None
    approved_segments_file: str | None
    json_report: str | None
    markdown_report: str | None
    finalized: bool
    dry_run: bool = False
    qa_errors_overridden: bool = False
    override_reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.error_count == 0 or self.qa_errors_overridden

    @property
    def ok(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        value["valid"] = self.valid
        value["ok"] = self.ok
        return value


@dataclass(frozen=True, slots=True)
class _ReviewContext:
    project_path: Path
    project_id: str
    target_language: str
    segments: tuple[TranslationSegment, ...]
    termbase_entries: tuple[dict[str, Any], ...]
    translation_state: dict[str, Any]
    translation_output_sha256: str
    termbase_sha256: str
    draft_sha256: str
    review_data: bytes
    review_sha256: str


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise TranslationReviewError(f"{label} not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranslationReviewError(f"Invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise TranslationReviewError(f"{label} must be a JSON object.")
    return value


def _load_segments(path: Path) -> tuple[TranslationSegment, ...]:
    if not path.is_file():
        raise TranslationReviewError(
            f"Translation segments not found: {path}. Run glk translate first."
        )
    segments: list[TranslationSegment] = []
    line_number = 0
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.strip():
                segments.append(TranslationSegment.from_dict(json.loads(line)))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TranslationSegmentValidationError,
        TypeError,
    ) as error:
        raise TranslationReviewError(
            f"Invalid translation segment JSONL at line {line_number}: {error}"
        ) from error
    if not segments:
        raise TranslationReviewError("Translation segment JSONL is empty.")
    ordered = sorted(segments, key=lambda item: item.source_order)
    if len({item.source_block_id for item in ordered}) != len(ordered):
        raise TranslationReviewError("Translation segment JSONL has duplicate block IDs.")
    return tuple(ordered)


def _load_active_termbase(path: Path) -> tuple[dict[str, Any], ...]:
    value = _load_json_object(path, "termbase")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise TranslationReviewError("Termbase must contain an entries array.")
    active: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise TranslationReviewError(f"Termbase entry {index} is not an object.")
        if entry.get("status") in {"approved", "keep"}:
            active.append(entry)
    return tuple(active)


def _term_variants(entry: dict[str, Any]) -> list[str]:
    source_term = str(entry.get("source_term") or "").strip()
    raw_variants = entry.get("variants")
    variants = (
        [str(value).strip() for value in raw_variants]
        if isinstance(raw_variants, list)
        else []
    )
    return list(
        dict.fromkeys(
            value
            for value in [source_term, *variants]
            if value
        )
    )


def _term_pattern(term: str) -> re.Pattern[str]:
    prefix = r"(?<!\w)" if term[0].isalnum() else ""
    suffix = r"(?!\w)" if term[-1].isalnum() else ""
    return re.compile(prefix + re.escape(term) + suffix, re.IGNORECASE)


def _term_matches(text: str, entry: dict[str, Any]) -> bool:
    return any(
        _term_pattern(variant).search(text)
        for variant in _term_variants(entry)
    )


def _review_term(entry: dict[str, Any]) -> TranslationReviewTerm:
    return {
        "source_term": str(entry.get("source_term") or ""),
        "translation": str(entry.get("translation") or ""),
        "status": str(entry.get("status") or ""),
        "category": str(entry.get("category") or ""),
        "variants": _term_variants(entry),
        "note": str(entry.get("note") or ""),
    }


def _relevant_review_terms(
    source_text: str,
    entries: tuple[dict[str, Any], ...],
) -> list[TranslationReviewTerm]:
    return [
        _review_term(entry)
        for entry in entries
        if _term_matches(source_text, entry)
    ]


def _source_latin_is_fully_kept(
    source_text: str,
    entries: tuple[dict[str, Any], ...],
) -> bool:
    remaining = source_text
    matched = False
    variants = sorted(
        {
            variant
            for entry in entries
            if entry.get("status") == "keep"
            for variant in _term_variants(entry)
        },
        key=len,
        reverse=True,
    )
    for variant in variants:
        remaining, count = _term_pattern(variant).subn("", remaining)
        matched = matched or count > 0
    return matched and _LATIN_PATTERN.search(remaining) is None


def _require_current_translation(
    project: str | Path,
    workspace_root: str | Path,
) -> tuple[Path, dict[str, Any], tuple[TranslationSegment, ...]]:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if pipeline["translation_status"] != "current":
        raise TranslationReviewError(
            "Translation draft is not current. Complete or refresh glk translate first."
        )
    state = _load_json_object(
        paths.translation_state, "translation state"
    )
    segments = _load_segments(paths.translation_segments)
    return location.path, state, segments


def prepare_project_translation_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    dry_run: bool = False,
) -> TranslationReviewPrepareResult:
    """Create or deliberately reset 04_translation/review.txt from the draft."""
    project_path, state, segments = _require_current_translation(
        project, workspace_root
    )
    paths = WorkspacePaths(project_path)
    draft_path = paths.translation_draft
    review_path = paths.translation_review
    if not draft_path.is_file():
        raise TranslationReviewError(f"Translation draft not found: {draft_path}")
    draft_data = draft_path.read_bytes()
    draft_hash = _sha256_bytes(draft_data)
    if state.get("draft_sha256") != draft_hash:
        raise TranslationReviewError(
            "Translation draft does not match its state. Run glk translate --force."
        )

    review_created = force or not review_path.is_file()
    if review_created:
        review_status = "current"
    elif (
        state.get("review_status") == "current"
        and state.get("review_base_draft_sha256") == draft_hash
    ):
        review_status = "current"
    else:
        review_status = "stale"

    if not dry_run and review_created:
        _write_bytes_atomic(review_path, draft_data)
        state.update(
            {
                "review_status": "current",
                "review_base_draft_sha256": draft_hash,
                "updated_at": _utc_now(),
            }
        )
        _write_json_atomic(paths.translation_state, state)

    return TranslationReviewPrepareResult(
        project_path=str(project_path),
        total_blocks=len(segments),
        draft_sha256=draft_hash,
        review_file=None if dry_run else str(review_path),
        review_created=review_created,
        review_status=review_status,
        dry_run=dry_run,
    )


def _locator(segment: TranslationSegment) -> tuple[str, str | int]:
    if segment.page is not None:
        return "page", segment.page
    return "source", segment.source_file


def _locator_line(segment: TranslationSegment) -> str:
    kind, value = _locator(segment)
    return f"[PAGE {value}]" if kind == "page" else f"[SOURCE {value}]"


def _skip_spacing(lines: list[str], index: int) -> int:
    while index < len(lines) and lines[index] in {"", _SEPARATOR}:
        index += 1
    return index


def _parse_review_text(
    data: bytes, segments: tuple[TranslationSegment, ...]
) -> dict[str, str]:
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise TranslationReviewParseError(
            "invalid_encoding", "번역 검수 TXT는 올바른 UTF-8 형식이어야 합니다."
        ) from error
    if not lines or lines[0] != _REVIEW_HEADER:
        raise TranslationReviewParseError(
            "invalid_header",
            "번역 검수 TXT의 머리말이 없거나 지원하지 않는 형식입니다.",
        )

    translations: dict[str, str] = {}
    index = 1
    previous_locator: tuple[str, str | int] | None = None
    for segment in segments:
        index = _skip_spacing(lines, index)
        current_locator = _locator(segment)
        if current_locator != previous_locator:
            expected = _locator_line(segment)
            found = lines[index] if index < len(lines) else "end of file"
            if found != expected:
                raise TranslationReviewParseError(
                    "location_changed",
                    (
                        f"{segment.source_block_id} block의 위치 표시가 변경되었습니다. "
                        f"기대값: {expected!r} / 현재값: {found!r}"
                    ),
                    segment.source_block_id,
                )
            index += 1
            previous_locator = current_locator

        expected_block = f"[BLOCK {segment.source_block_id}]"
        found = lines[index] if index < len(lines) else "end of file"
        if found != expected_block:
            match = _BLOCK_PATTERN.fullmatch(found) if isinstance(found, str) else None
            detail = (
                f"현재 block: {match.group(1)}"
                if match
                else f"현재값: {found!r}"
            )
            raise TranslationReviewParseError(
                "block_order_changed",
                (
                    "block 표시 순서가 올바르지 않습니다. "
                    f"기대 block: {segment.source_block_id} / {detail}"
                ),
                segment.source_block_id,
            )
        index += 1
        if index >= len(lines) or lines[index] != "[ORIGINAL]":
            raise TranslationReviewParseError(
                "original_marker_changed",
                f"{segment.source_block_id} block의 [ORIGINAL] 표시가 변경되었습니다.",
                segment.source_block_id,
            )
        index += 1

        expected_source_lines = segment.source_text.splitlines()
        found_source_lines = lines[index : index + len(expected_source_lines)]
        if found_source_lines != expected_source_lines:
            raise TranslationReviewParseError(
                "source_changed",
                (
                    f"{segment.source_block_id} block의 원문이 변경되었습니다. "
                    "[TRANSLATION] 아래의 번역문만 수정하세요."
                ),
                segment.source_block_id,
            )
        index += len(expected_source_lines)
        if index >= len(lines) or lines[index] != "[TRANSLATION]":
            raise TranslationReviewParseError(
                "translation_marker_changed",
                f"{segment.source_block_id} block의 [TRANSLATION] 표시가 변경되었습니다.",
                segment.source_block_id,
            )
        index += 1
        end_marker = f"[[GLK_END {segment.source_block_id}]]"
        try:
            end_index = lines.index(end_marker, index)
        except ValueError as error:
            raise TranslationReviewParseError(
                "end_marker_changed",
                f"{segment.source_block_id} block의 끝 표시가 변경되거나 삭제되었습니다.",
                segment.source_block_id,
            ) from error
        translations[segment.source_block_id] = "\n".join(
            lines[index:end_index]
        ).strip()
        index = end_index + 1

    index = _skip_spacing(lines, index)
    if index != len(lines):
        found = lines[index]
        match = _BLOCK_PATTERN.fullmatch(found)
        code = "unknown_block" if match else "unexpected_content"
        message = (
            f"번역 검수 TXT에 알 수 없거나 중복된 block이 있습니다: {match.group(1)}"
            if match
            else f"마지막 block 뒤에 예상하지 못한 내용이 있습니다: {found!r}"
        )
        raise TranslationReviewParseError(code, message)
    return translations


def _render_review_text(
    segments: tuple[TranslationSegment, ...],
    translations: dict[str, str],
) -> bytes:
    lines = [_REVIEW_HEADER, ""]
    previous_locator: tuple[str, str | int] | None = None
    for segment in segments:
        locator = _locator(segment)
        if locator != previous_locator:
            if previous_locator is not None:
                lines.extend(["", _SEPARATOR, ""])
            lines.append(_locator_line(segment))
            previous_locator = locator
        lines.extend(
            [
                f"[BLOCK {segment.source_block_id}]",
                "[ORIGINAL]",
                segment.source_text,
                "[TRANSLATION]",
                translations[segment.source_block_id],
                f"[[GLK_END {segment.source_block_id}]]",
                "",
            ]
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _load_review_context(
    project: str | Path,
    workspace_root: str | Path,
) -> _ReviewContext:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if pipeline["translation_status"] != "current":
        raise TranslationReviewError(
            "Translation draft is not current. Complete or refresh glk translate first."
        )
    state = _load_json_object(
        paths.translation_state, "translation state"
    )
    translation_path = paths.translation_segments
    translation_data = translation_path.read_bytes()
    translation_hash = _sha256_bytes(translation_data)
    if state.get("translation_output_sha256") != translation_hash:
        raise TranslationReviewError("Translation segments do not match their state.")
    segments = _load_segments(translation_path)

    draft_path = paths.translation_draft
    if not draft_path.is_file():
        raise TranslationReviewError(f"Translation draft not found: {draft_path}")
    draft_hash = _sha256_bytes(draft_path.read_bytes())
    if state.get("draft_sha256") != draft_hash:
        raise TranslationReviewError("Translation draft does not match its state.")
    if (
        state.get("review_status") != "current"
        or state.get("review_base_draft_sha256") != draft_hash
    ):
        raise TranslationReviewError(
            "Translation review is stale. Compare it with the draft, then run "
            "glk translation prepare --force to reset it deliberately."
        )

    review_path = paths.translation_review
    if not review_path.is_file():
        raise TranslationReviewError(
            f"Translation review TXT not found: {review_path}. "
            "Run glk translation prepare first."
        )
    review_data = review_path.read_bytes()
    termbase_path = paths.termbase
    return _ReviewContext(
        project_path=location.path,
        project_id=location.manifest.project_id,
        target_language=location.manifest.target_language,
        segments=segments,
        termbase_entries=_load_active_termbase(termbase_path),
        translation_state=state,
        translation_output_sha256=translation_hash,
        termbase_sha256=_sha256_bytes(termbase_path.read_bytes()),
        draft_sha256=draft_hash,
        review_data=review_data,
        review_sha256=_sha256_bytes(review_data),
    )


def _analyze_review(
    context: _ReviewContext,
) -> tuple[dict[str, str], tuple[TranslationReviewIssue, ...]]:
    try:
        translations = _parse_review_text(context.review_data, context.segments)
    except TranslationReviewParseError as error:
        return {}, (
            TranslationReviewIssue(
                severity="error",
                code=error.code,
                block_id=error.block_id,
                message=str(error),
            ),
        )

    issues: list[TranslationReviewIssue] = []
    for segment in context.segments:
        translated = translations[segment.source_block_id]
        if not translated:
            issues.append(
                TranslationReviewIssue(
                    severity="error",
                    code="empty_translation",
                    block_id=segment.source_block_id,
                    message="번역문이 비어 있습니다.",
                )
            )
            continue
        if "�" in translated:
            issues.append(
                TranslationReviewIssue(
                    severity="error",
                    code="replacement_character",
                    block_id=segment.source_block_id,
                    message=(
                        "번역문에 깨진 문자를 나타내는 Unicode 대체 문자(�)가 "
                        "포함되어 있습니다."
                    ),
                )
            )
        if "[ILLEGIBLE]" in translated.upper():
            issues.append(
                TranslationReviewIssue(
                    severity="error",
                    code="unresolved_illegible",
                    block_id=segment.source_block_id,
                    message="번역문에 판독 불가 표시 [ILLEGIBLE]이 남아 있습니다.",
                )
            )
        for issue in check_translation_contract(
            source_text=segment.source_text,
            translated_text=translated,
            termbase_entries=list(context.termbase_entries),
        ):
            issues.append(
                TranslationReviewIssue(
                    severity="error",
                    code=issue.code,
                    block_id=segment.source_block_id,
                    message=issue.message,
                )
            )
        fully_kept = _source_latin_is_fully_kept(
            segment.source_text,
            context.termbase_entries,
        )
        if (
            fully_kept
            and _LATIN_PATTERN.search(segment.source_text)
            and not _HANGUL_PATTERN.search(translated)
        ):
            issues.append(
                TranslationReviewIssue(
                    severity="info",
                    code="keep_rule_applied",
                    block_id=segment.source_block_id,
                    message=(
                        "이 블록의 영문은 용어집의 원문 유지 규칙으로 "
                        "보존되었습니다."
                    ),
                )
            )
        elif translated == segment.source_text and _LATIN_PATTERN.search(
            segment.source_text
        ):
            issues.append(
                TranslationReviewIssue(
                    severity="warning",
                    code="unchanged_translation",
                    block_id=segment.source_block_id,
                    message="번역문이 원문과 완전히 같아 미번역 문장인지 확인해야 합니다.",
                )
            )
        elif (
            context.target_language.casefold() == "ko"
            and _LATIN_PATTERN.search(segment.source_text)
            and not _HANGUL_PATTERN.search(translated)
        ):
            issues.append(
                TranslationReviewIssue(
                    severity="warning",
                    code="target_script_missing",
                    block_id=segment.source_block_id,
                    message="한국어 번역문에 한글이 없어 미번역 문장인지 확인해야 합니다.",
                )
            )
    return translations, tuple(issues)


def get_project_translation_review_document(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
) -> TranslationReviewDocument:
    """Build the safe view model consumed by the local review UI."""
    context = _load_review_context(project, workspace_root)
    translations, issues = _analyze_review(context)
    if not translations:
        issue = issues[0] if issues else None
        detail = issue.message if issue else "번역 검수 TXT를 해석할 수 없습니다."
        raise TranslationReviewError(
            f"{detail} Reset it only after comparison with "
            "glk translation prepare --force."
        )
    issue_map: dict[str, list[TranslationReviewIssuePayload]] = {}
    general_issues: list[TranslationReviewIssuePayload] = []
    for issue in issues:
        value = issue.to_dict()
        if issue.block_id is None:
            general_issues.append(value)
        else:
            issue_map.setdefault(issue.block_id, []).append(value)
    errors, warnings, information = _issue_counts(issues)
    overridable_errors, blocking_errors = _overridable_error_counts(issues)
    location = load_project(project, workspace_root)
    pipeline = inspect_project(location.path)["pipeline"]
    termbase = [_review_term(entry) for entry in context.termbase_entries]
    blocks: list[TranslationReviewBlock] = []
    for segment in context.segments:
        translation = translations[segment.source_block_id]
        blocks.append(
            {
                "id": segment.source_block_id,
                "source_file": segment.source_file,
                "page": segment.page,
                "source_order": segment.source_order,
                "block_type": segment.block_type,
                "source": segment.source_text,
                "draft_translation": segment.translated_text,
                "translation": translation,
                "changed": translation != segment.translated_text,
                "issues": issue_map.get(segment.source_block_id, []),
                "relevant_terms": _relevant_review_terms(
                    segment.source_text,
                    context.termbase_entries,
                ),
            }
        )
    return {
        "schema_version": 1,
        "project": {
            "id": location.manifest.project_id,
            "name": location.manifest.name,
            "source_language": location.manifest.source_language,
            "target_language": location.manifest.target_language,
        },
        "review_sha256": context.review_sha256,
        "review_status": pipeline["translation_review"],
        "final_translation_approved": pipeline["final_translation_approved"],
        "summary": {
            "blocks": len(context.segments),
            "changed": sum(block["changed"] for block in blocks),
            "errors": errors,
            "overridable_errors": overridable_errors,
            "blocking_errors": blocking_errors,
            "warnings": warnings,
            "info": information,
            "passed": errors == 0,
        },
        "general_issues": general_issues,
        "termbase": termbase,
        "blocks": blocks,
    }


def save_project_translation_review(
    *,
    project: str | Path,
    translations: dict[str, Any],
    expected_review_sha256: str,
    workspace_root: str | Path = "workspaces",
) -> TranslationReviewDocument:
    """Safely rebuild review TXT from block translations with optimistic locking."""
    context = _load_review_context(project, workspace_root)
    if expected_review_sha256 != context.review_sha256:
        raise TranslationReviewConflictError(
            "The review TXT changed after this page was loaded. Reload before saving."
        )
    if not isinstance(translations, dict):
        raise TranslationReviewError("translations must be an object keyed by block ID.")
    expected_ids = [segment.source_block_id for segment in context.segments]
    submitted_ids = list(translations)
    missing = [block_id for block_id in expected_ids if block_id not in translations]
    extra = [block_id for block_id in submitted_ids if block_id not in expected_ids]
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing[:5]))
        if extra:
            details.append("unknown: " + ", ".join(extra[:5]))
        raise TranslationReviewBlockMismatchError(
            "Submitted translation block IDs do not match the current review ("
            + "; ".join(details)
            + ")."
        )

    normalized: dict[str, str] = {}
    for segment in context.segments:
        value = translations[segment.source_block_id]
        if not isinstance(value, str):
            raise TranslationReviewError(
                f"Translation for {segment.source_block_id} must be text."
            )
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text.encode("utf-8")) > 1_000_000:
            raise TranslationReviewError(
                f"Translation for {segment.source_block_id} is too large."
            )
        reserved = f"[[GLK_END {segment.source_block_id}]]"
        if reserved in text.splitlines():
            raise TranslationReviewError(
                f"Translation for {segment.source_block_id} contains a reserved marker."
            )
        normalized[segment.source_block_id] = text

    review_data = _render_review_text(context.segments, normalized)
    paths = WorkspacePaths(context.project_path)
    _write_bytes_atomic(paths.translation_review, review_data)
    return get_project_translation_review_document(
        project=context.project_path,
        workspace_root=workspace_root,
    )


def _issue_counts(
    issues: tuple[TranslationReviewIssue, ...],
) -> tuple[int, int, int]:
    return (
        sum(issue.severity == "error" for issue in issues),
        sum(issue.severity == "warning" for issue in issues),
        sum(issue.severity == "info" for issue in issues),
    )


def _overridable_error_counts(
    issues: tuple[TranslationReviewIssue, ...],
) -> tuple[int, int]:
    overridable = sum(
        issue.severity == "error"
        and issue.code in _OVERRIDABLE_QA_ERROR_CODES
        for issue in issues
    )
    errors = sum(issue.severity == "error" for issue in issues)
    return overridable, errors - overridable


def _report_payload(
    context: _ReviewContext,
    issues: tuple[TranslationReviewIssue, ...],
) -> dict[str, Any]:
    errors, warnings, information = _issue_counts(issues)
    return {
        "schema_version": 1,
        "version": TRANSLATION_REVIEW_VERSION,
        "project_id": context.project_id,
        "translation_output_sha256": context.translation_output_sha256,
        "termbase_sha256": context.termbase_sha256,
        "draft_sha256": context.draft_sha256,
        "review_sha256": context.review_sha256,
        "total_blocks": len(context.segments),
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "info": information,
            "passed": errors == 0,
        },
        "issues": [issue.to_dict() for issue in issues],
        "generated_at": _utc_now(),
    }


def _markdown_report(payload: dict[str, Any]) -> bytes:
    summary = payload["summary"]
    severity_labels = {
        "error": "오류",
        "warning": "경고",
        "info": "참고",
    }
    lines = [
        "# 번역 QA 보고서",
        "",
        f"- 프로젝트: `{payload['project_id']}`",
        f"- 전체 block: {payload['total_blocks']}",
        f"- 오류: {summary['errors']}",
        f"- 경고: {summary['warnings']}",
        f"- 결과: {'통과' if summary['passed'] else '실패'}",
        "",
    ]
    issues = payload["issues"]
    if not issues:
        lines.append("로컬 규칙에서 발견된 문제가 없습니다.")
    else:
        lines.extend(
            [
                "| 심각도 | 코드 | Block | 사유 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for issue in issues:
            message = issue["message"].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {severity_labels.get(issue['severity'], issue['severity'])} | "
                f"`{issue['code']}` | "
                f"`{issue['block_id'] or '-'}` | {message} |"
            )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _write_qa_artifacts(
    context: _ReviewContext,
    issues: tuple[TranslationReviewIssue, ...],
    *,
    status: str,
) -> tuple[Path, Path, dict[str, Any]]:
    payload = _report_payload(context, issues)
    json_data = (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    markdown_data = _markdown_report(payload)
    paths = WorkspacePaths(context.project_path)
    json_path = paths.translation_qa_json
    markdown_path = paths.translation_qa_markdown
    _write_bytes_atomic(json_path, json_data)
    _write_bytes_atomic(markdown_path, markdown_data)
    errors, warnings, information = _issue_counts(issues)
    state = {
        "schema_version": 1,
        "version": TRANSLATION_REVIEW_VERSION,
        "status": status,
        "translation_output_sha256": context.translation_output_sha256,
        "termbase_sha256": context.termbase_sha256,
        "draft_sha256": context.draft_sha256,
        "review_sha256": context.review_sha256,
        "total_blocks": len(context.segments),
        "error_count": errors,
        "warning_count": warnings,
        "info_count": information,
        "qa_json_file": paths.relative(paths.translation_qa_json),
        "qa_json_sha256": _sha256_bytes(json_data),
        "qa_markdown_file": paths.relative(paths.translation_qa_markdown),
        "qa_markdown_sha256": _sha256_bytes(markdown_data),
        "updated_at": _utc_now(),
    }
    _write_json_atomic(paths.translation_review_state, state)
    return json_path, markdown_path, state


def run_project_translation_qa(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    dry_run: bool = False,
) -> TranslationQaResult:
    """Inspect the edited translation review and optionally write QA reports."""
    context = _load_review_context(project, workspace_root)
    _, issues = _analyze_review(context)
    errors, warnings, information = _issue_counts(issues)
    json_path: Path | None = None
    markdown_path: Path | None = None
    if not dry_run:
        json_path, markdown_path, _ = _write_qa_artifacts(
            context,
            issues,
            status="qa_passed" if errors == 0 else "qa_failed",
        )
    return TranslationQaResult(
        project_path=str(context.project_path),
        total_blocks=len(context.segments),
        error_count=errors,
        warning_count=warnings,
        info_count=information,
        issues=issues,
        json_report=str(json_path) if json_path else None,
        markdown_report=str(markdown_path) if markdown_path else None,
        dry_run=dry_run,
    )


def _approved_segments(
    context: _ReviewContext, translations: dict[str, str]
) -> list[ApprovedTranslationSegment]:
    approved: list[ApprovedTranslationSegment] = []
    for segment in context.segments:
        reviewed = translations[segment.source_block_id]
        corrected = reviewed if reviewed != segment.translated_text else None
        final_hash = _sha256_bytes(reviewed.encode("utf-8"))
        item = ApprovedTranslationSegment(
            schema_version=APPROVED_TRANSLATION_SCHEMA_VERSION,
            source_block_id=segment.source_block_id,
            source_file=segment.source_file,
            page=segment.page,
            source_order=segment.source_order,
            block_type=segment.block_type,
            source_text=segment.source_text,
            source_sha256=segment.source_sha256,
            draft_translation=segment.translated_text,
            draft_translation_sha256=segment.translation_sha256,
            corrected_translation=corrected,
            final_translation_sha256=final_hash,
            status="approved",
            model=segment.model,
            prompt_sha256=segment.prompt_sha256,
            termbase_sha256=segment.termbase_sha256,
        )
        item.validate()
        approved.append(item)
    return approved


def _serialize_approved(segments: list[ApprovedTranslationSegment]) -> bytes:
    return "".join(
        json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in segments
    ).encode("utf-8")


def _render_final_translation(
    segments: list[ApprovedTranslationSegment],
) -> bytes:
    lines: list[str] = []
    previous_locator: tuple[str, str | int] | None = None
    for segment in sorted(segments, key=lambda item: item.source_order):
        locator = (
            ("page", segment.page)
            if segment.page is not None
            else ("source", segment.source_file)
        )
        if locator != previous_locator:
            if previous_locator is not None:
                lines.extend((_FINAL_SECTION_SEPARATOR, ""))
            lines.append(
                f"[PAGE {locator[1]}]"
                if locator[0] == "page"
                else f"[{PurePosixPath(str(locator[1])).name}]"
            )
            lines.append("")
            previous_locator = locator
        lines.append(segment.effective_translation.strip())
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _render_translation_text_only(
    segments: list[ApprovedTranslationSegment],
) -> bytes:
    lines: list[str] = []
    for segment in sorted(segments, key=lambda item: item.source_order):
        lines.extend((segment.effective_translation.strip(), ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _final_translation_outputs(
    paths: WorkspacePaths,
    project_source_file: str | None,
    segments: list[ApprovedTranslationSegment],
) -> dict[Path, bytes]:
    combined_path = paths.final_translation_for(project_source_file)
    outputs = {combined_path: _render_final_translation(segments)}
    if is_pdf_source_file(project_source_file):
        return outputs

    by_source: dict[str, list[ApprovedTranslationSegment]] = {}
    for segment in segments:
        if not segment.source_file.startswith(f"{IMAGE_SOURCE_ROOT}/"):
            raise TranslationReviewError(
                f"이미지 원본 경로가 올바르지 않습니다: {segment.source_file}"
            )
        by_source.setdefault(segment.source_file, []).append(segment)
    for source_file, source_segments in by_source.items():
        output_path = paths.final_image_translation_for(source_file)
        if output_path in outputs:
            raise TranslationReviewError(
                "서로 다른 이미지의 최종 번역 파일명이 충돌합니다: "
                f"{paths.relative(output_path)}"
            )
        outputs[output_path] = _render_translation_text_only(source_segments)
    return outputs


def finalize_project_translation_review(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    dry_run: bool = False,
    qa_override_reason: str | None = None,
) -> TranslationFinalizeResult:
    """Promote a safe review, optionally acknowledging semantic QA errors."""
    context = _load_review_context(project, workspace_root)
    translations, issues = _analyze_review(context)
    errors, warnings, _ = _issue_counts(issues)
    overridable_errors, blocking_errors = _overridable_error_counts(issues)
    override_reason = (
        qa_override_reason.strip()
        if isinstance(qa_override_reason, str)
        else ""
    )
    if len(override_reason) > _MAX_QA_OVERRIDE_REASON_LENGTH:
        raise TranslationReviewError(
            "QA override reason must be 1000 characters or fewer."
        )
    override_requested = bool(override_reason)
    if override_requested and not errors:
        raise TranslationReviewError(
            "QA override can only be used when review errors remain."
        )
    if override_requested and blocking_errors:
        raise TranslationReviewError(
            "Structural or protected-content QA errors cannot be overridden. "
            "Resolve every non-overridable error before final approval."
        )
    qa_errors_overridden = bool(
        override_requested and errors and overridable_errors == errors
    )
    json_path: Path | None = None
    markdown_path: Path | None = None
    state: dict[str, Any] | None = None
    if not dry_run:
        json_path, markdown_path, state = _write_qa_artifacts(
            context,
            issues,
            status="qa_passed" if errors == 0 else "qa_failed",
        )

    if errors and not qa_errors_overridden:
        return TranslationFinalizeResult(
            project_path=str(context.project_path),
            total_blocks=len(context.segments),
            changed_blocks=0,
            error_count=errors,
            warning_count=warnings,
            issues=issues,
            output_file=None,
            approved_segments_file=None,
            json_report=str(json_path) if json_path else None,
            markdown_report=str(markdown_path) if markdown_path else None,
            finalized=False,
            dry_run=dry_run,
        )

    approved = _approved_segments(context, translations)
    changed_blocks = sum(item.corrected_translation is not None for item in approved)
    approved_data = _serialize_approved(approved)
    paths = WorkspacePaths(context.project_path)
    approved_path = paths.approved_translation_segments
    location = load_project(context.project_path, workspace_root)
    project_source_file = location.manifest.source_file
    if project_source_file is None:
        source_files = {segment.source_file for segment in approved}
        if len(source_files) == 1 and is_pdf_source_file(next(iter(source_files))):
            project_source_file = next(iter(source_files))
        elif source_files and all(
            source_file.startswith(f"{IMAGE_SOURCE_ROOT}/")
            for source_file in source_files
        ):
            project_source_file = IMAGE_SOURCE_ROOT
    final_outputs = _final_translation_outputs(
        paths,
        project_source_file,
        approved,
    )
    final_path = paths.final_translation_for(project_source_file)
    if not dry_run:
        _write_bytes_atomic(approved_path, approved_data)
        for output_path, output_data in final_outputs.items():
            _write_bytes_atomic(output_path, output_data)
        paths.final_translation.unlink(missing_ok=True)
        if state is None:
            raise TranslationReviewError("Translation review state was not created.")
        final_files = {
            paths.relative(output_path): _sha256_bytes(output_data)
            for output_path, output_data in sorted(
                final_outputs.items(), key=lambda item: str(item[0])
            )
        }
        state.update(
            {
                "status": "approved",
                "changed_blocks": changed_blocks,
                "approved_segments_file": paths.relative(
                    paths.approved_translation_segments
                ),
                "approved_segments_sha256": _sha256_bytes(approved_data),
                "final_file": paths.relative(final_path),
                "final_sha256": final_files[paths.relative(final_path)],
                "final_files": final_files,
                "approved_at": _utc_now(),
            }
        )
        if qa_errors_overridden:
            state["qa_override"] = {
                "reason": override_reason,
                "review_sha256": context.review_sha256,
                "error_count": errors,
                "issues": [
                    issue.to_dict()
                    for issue in issues
                    if issue.severity == "error"
                ],
                "approved_at": state["approved_at"],
            }
        _write_json_atomic(
            paths.translation_review_state, state
        )

    return TranslationFinalizeResult(
        project_path=str(context.project_path),
        total_blocks=len(context.segments),
        changed_blocks=changed_blocks,
        error_count=errors,
        warning_count=warnings,
        issues=issues,
        output_file=None if dry_run else str(final_path),
        approved_segments_file=None if dry_run else str(approved_path),
        json_report=str(json_path) if json_path else None,
        markdown_report=str(markdown_path) if markdown_path else None,
        finalized=not dry_run,
        dry_run=dry_run,
        qa_errors_overridden=qa_errors_overridden,
        override_reason=override_reason or None,
    )
