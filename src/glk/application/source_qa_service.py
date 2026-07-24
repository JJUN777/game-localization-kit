"""Run deterministic, local-only QA rules against review-source blocks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from glk.application.project_service import load_project
from glk.domain.source_block import SourceBlock, SourceBlockValidationError
from glk.domain.workspace import WorkspacePaths
from glk.domain.source_qa import SOURCE_QA_SCHEMA_VERSION, SourceQaIssue


SOURCE_QA_VERSION = "source-qa-local-v5"
_TOKEN_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
_UNRESOLVED_ICON_PATTERN = re.compile(r"\[ICON:\s*[^\]]+\]", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[/-][A-Za-z0-9]+)*(?![A-Za-z0-9])")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*$")
_IMAGE_FILENAME_PATTERN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)\.(?:png|jpe?g|webp|gif|bmp)\b",
    re.IGNORECASE,
)


class SourceQaError(ValueError):
    """Raised when source QA input or cache is invalid."""


@dataclass(frozen=True, slots=True)
class SourceQaResult:
    project_path: str
    input_sha256: str
    total_blocks: int
    flagged_blocks: int
    total_issues: int
    error_count: int
    warning_count: int
    info_count: int
    allowed_tokens: tuple[str, ...]
    output_file: str | None
    human_report_file: str | None = None
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


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _load_blocks(path: Path) -> tuple[list[SourceBlock], bytes]:
    if not path.is_file():
        raise SourceQaError(
            f"Review-source blocks not found: {path}. Run glk segment first."
        )
    data = path.read_bytes()
    blocks: list[SourceBlock] = []
    line_number = 0
    try:
        for line_number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            blocks.append(SourceBlock.from_dict(json.loads(line)))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceBlockValidationError,
        TypeError,
    ) as error:
        raise SourceQaError(
            f"Invalid source block JSONL at line {line_number}: {error}"
        ) from error
    if not blocks:
        raise SourceQaError("Review-source block JSONL is empty.")
    ids = [block.id for block in blocks]
    if len(ids) != len(set(ids)):
        raise SourceQaError("Review-source block JSONL contains duplicate block IDs.")
    expected_order = list(range(1, len(blocks) + 1))
    if [block.source_order for block in blocks] != expected_order:
        raise SourceQaError("Review-source block source_order is not contiguous.")
    return blocks, data


def _load_allowed_tokens(project_path: Path) -> tuple[tuple[str, ...], bytes]:
    prompt_path = WorkspacePaths(project_path).input_ocr_prompt
    if not prompt_path.is_file():
        return (), b""
    data = prompt_path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceQaError(f"OCR prompt is not valid UTF-8: {prompt_path}") from error
    return tuple(sorted(set(_TOKEN_PATTERN.findall(text)))), data


def _qa_input_hash(source_data: bytes, prompt_data: bytes) -> str:
    digest = hashlib.sha256()
    for label, data in ((b"source", source_data), (b"prompt", prompt_data)):
        digest.update(label)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    digest.update(SOURCE_QA_VERSION.encode("utf-8"))
    return digest.hexdigest()


def _evidence(value: str, limit: int = 240) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _issue(
    block: SourceBlock,
    *,
    severity: str,
    code: str,
    message: str,
    evidence: str,
) -> SourceQaIssue:
    clean_evidence = _evidence(evidence)
    locator = f"{block.id}|{code}|{clean_evidence}"
    issue_id = f"qa-{_sha256_bytes(locator.encode('utf-8'))[:16]}"
    issue = SourceQaIssue(
        schema_version=SOURCE_QA_SCHEMA_VERSION,
        id=issue_id,
        block_id=block.id,
        severity=severity,
        code=code,
        message=message,
        evidence=clean_evidence,
        source_file=block.source_file,
        page=block.page,
        bbox=block.bbox,
        auto_fixable=False,
    )
    issue.validate()
    return issue


def _block_issues(block: SourceBlock, allowed_tokens: set[str]) -> list[SourceQaIssue]:
    issues: list[SourceQaIssue] = []
    text = block.effective_text

    def add(severity: str, code: str, message: str, evidence: str = text) -> None:
        issues.append(
            _issue(
                block,
                severity=severity,
                code=code,
                message=message,
                evidence=evidence,
            )
        )

    expected_hash = "sha256:" + _sha256_bytes(block.raw_text.encode("utf-8"))
    if block.source_hash != expected_hash:
        add(
            "error",
            "SOURCE_HASH_MISMATCH",
            "저장된 원문과 무결성 hash가 일치하지 않습니다.",
        )
    if block.legibility == "uncertain":
        add("error", "OCR_UNCERTAIN", "OCR이 이 block을 판독 불확실로 표시했습니다.")
    if block.warnings:
        add(
            "warning",
            "SOURCE_WARNING",
            "원문 추출 과정에서 이 block에 대한 경고가 발생했습니다.",
            " | ".join(block.warnings),
        )
    if "[ILLEGIBLE]" in text.upper():
        add(
            "error",
            "OCR_ILLEGIBLE",
            "원문에 판독 불가 표시 [ILLEGIBLE]이 포함되어 있습니다.",
        )
    unresolved_icons = _UNRESOLVED_ICON_PATTERN.findall(text)
    if unresolved_icons:
        add(
            "warning",
            "ICON_UNRESOLVED",
            "알려진 token으로 변환되지 않은 아이콘 표시가 있습니다.",
            ", ".join(unresolved_icons),
        )
    if "�" in text:
        add(
            "error",
            "UNICODE_REPLACEMENT",
            "원문에 깨진 문자를 나타내는 Unicode 대체 문자(�)가 있습니다.",
        )

    valid_token_spans = [match.span() for match in _TOKEN_PATTERN.finditer(text)]
    token_stripped = text
    for start, end in reversed(valid_token_spans):
        token_stripped = token_stripped[:start] + token_stripped[end:]
    if "{" in token_stripped or "}" in token_stripped:
        add("error", "TOKEN_MALFORMED", "중괄호 token 형식이 올바르지 않습니다.")

    tokens = _TOKEN_PATTERN.findall(text)
    if allowed_tokens:
        for token in sorted(set(tokens) - allowed_tokens):
            add(
                "warning",
                "TOKEN_UNKNOWN",
                f"Token {{{token}}}이 OCR prompt에 정의되어 있지 않습니다.",
                f"{{{token}}}",
            )
    for filename_token in _IMAGE_FILENAME_PATTERN.findall(text):
        add(
            "warning",
            "ICON_FILENAME_LITERAL",
            "아이콘이 중괄호 token 대신 이미지 파일명으로 인식되었습니다.",
            filename_token,
        )

    ambiguous = sorted(
        {
            candidate
            for candidate in _WORD_PATTERN.findall(text)
            if any(character.isdigit() for character in candidate)
            and any(character in "OIl" for character in candidate)
        }
    )
    if ambiguous:
        add(
            "warning",
            "OCR_ALNUM_CONFUSION",
            "영문·숫자 조합에 O/0 또는 I/l/1 OCR 혼동이 의심됩니다.",
            ", ".join(ambiguous),
        )

    is_token_only = _TOKEN_PATTERN.fullmatch(text) is not None
    if block.block_type.casefold() == "identifier" and not is_token_only:
        if not _IDENTIFIER_PATTERN.fullmatch(text):
            add(
                "warning",
                "IDENTIFIER_FORMAT",
                "식별자에 예상하지 못한 공백이나 문장부호가 있습니다.",
            )
    if "\t" in text or re.search(r" {3,}", text):
        add(
            "info",
            "WHITESPACE_SUSPICIOUS",
            "탭 또는 세 칸 이상의 연속 공백이 있습니다.",
        )
    if len(text) > 5000:
        add(
            "warning",
            "BLOCK_TOO_LONG",
            "block이 비정상적으로 길어 읽기 순서가 다른 내용이 합쳐졌을 수 있습니다.",
        )
    if block.status == "flagged" and not any(
        issue.severity in {"error", "warning"} for issue in issues
    ):
        add(
            "warning",
            "SOURCE_PRE_FLAGGED",
            "원문 추출 단계에서 사람의 확인이 필요한 block으로 표시되었습니다.",
        )
    return issues


def run_local_source_qa(
    blocks: list[SourceBlock], allowed_tokens: tuple[str, ...]
) -> list[SourceQaIssue]:
    token_set = set(allowed_tokens)
    issues: list[SourceQaIssue] = []
    identifier_blocks: dict[str, list[SourceBlock]] = {}
    for block in blocks:
        issues.extend(_block_issues(block, token_set))
        if (
            block.block_type.casefold() == "identifier"
            and _TOKEN_PATTERN.fullmatch(block.effective_text) is None
        ):
            identifier_blocks.setdefault(block.effective_text, []).append(block)
    for identifier, matches in identifier_blocks.items():
        if len(matches) <= 1:
            continue
        for block in matches:
            issues.append(
                _issue(
                    block,
                    severity="warning",
                    code="IDENTIFIER_DUPLICATE",
                    message="같은 식별자가 둘 이상의 원문 block에 나타납니다.",
                    evidence=identifier,
                )
            )
    source_order = {block.id: block.source_order for block in blocks}
    return sorted(
        issues,
        key=lambda item: (source_order[item.block_id], item.code, item.id),
    )


def _render_markdown_report(
    *,
    total_blocks: int,
    issues: list[SourceQaIssue],
    severity_counts: dict[str, int],
) -> bytes:
    severity_labels = {
        "error": "오류",
        "warning": "경고",
        "info": "참고",
    }
    flagged_blocks = len(
        {issue.block_id for issue in issues if issue.severity in {"error", "warning"}}
    )
    lines = [
        "# 원문 QA 보고서",
        "",
        f"- 전체 블록: {total_blocks}",
        f"- 확인 필요 블록: {flagged_blocks}",
        f"- 오류: {severity_counts['error']}",
        f"- 경고: {severity_counts['warning']}",
        f"- 참고: {severity_counts['info']}",
        "",
    ]
    if not issues:
        lines.extend(("로컬 규칙에서 발견된 의심 항목이 없습니다.", ""))
    for number, issue in enumerate(issues, start=1):
        location = f"PDF {issue.page}페이지" if issue.page else issue.source_file
        evidence = " ".join(issue.evidence.split())
        lines.extend(
            (
                f"## Q-{number:04d} · {issue.code}",
                "",
                f"- 심각도: {severity_labels.get(issue.severity, issue.severity)}",
                f"- 위치: {location}",
                f"- 원본: `{issue.source_file}`",
                f"- 블록: `{issue.block_id}`",
                f"- 사유: {issue.message}",
                f"- 확인할 내용: `{evidence.replace('`', "'")}`",
                "",
            )
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _result_from_state(
    state: dict[str, Any],
    project_path: Path,
    output_path: Path,
    human_report_path: Path,
) -> SourceQaResult:
    return SourceQaResult(
        project_path=str(project_path),
        input_sha256=state["input_sha256"],
        total_blocks=int(state["total_blocks"]),
        flagged_blocks=int(state["flagged_blocks"]),
        total_issues=int(state["total_issues"]),
        error_count=int(state["issue_counts"]["error"]),
        warning_count=int(state["issue_counts"]["warning"]),
        info_count=int(state["issue_counts"]["info"]),
        allowed_tokens=tuple(state["allowed_tokens"]),
        output_file=str(output_path),
        human_report_file=str(human_report_path),
        cached=True,
    )


def run_project_source_qa(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    dry_run: bool = False,
) -> SourceQaResult:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    source_path = paths.source_segments
    blocks, source_data = _load_blocks(source_path)
    allowed_tokens, prompt_data = _load_allowed_tokens(location.path)
    input_hash = _qa_input_hash(source_data, prompt_data)
    issues = run_local_source_qa(blocks, allowed_tokens)
    severity_counts = {
        severity: sum(issue.severity == severity for issue in issues)
        for severity in ("error", "warning", "info")
    }
    flagged_ids = {
        issue.block_id for issue in issues if issue.severity in {"error", "warning"}
    }
    output_path = paths.source_qa_json
    human_report_path = paths.source_qa_markdown
    state_path = paths.source_qa_state
    if dry_run:
        return SourceQaResult(
            project_path=str(location.path),
            input_sha256=input_hash,
            total_blocks=len(blocks),
            flagged_blocks=len(flagged_ids),
            total_issues=len(issues),
            error_count=severity_counts["error"],
            warning_count=severity_counts["warning"],
            info_count=severity_counts["info"],
            allowed_tokens=allowed_tokens,
            output_file=None,
            human_report_file=None,
            dry_run=True,
        )

    if (
        not force
        and state_path.is_file()
        and output_path.is_file()
        and human_report_path.is_file()
    ):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                state.get("status") == "complete"
                and state.get("version") == SOURCE_QA_VERSION
                and state.get("input_sha256") == input_hash
                and state.get("output_sha256")
                == _sha256_bytes(output_path.read_bytes())
                and state.get("human_report_sha256")
                == _sha256_bytes(human_report_path.read_bytes())
            ):
                return _result_from_state(
                    state, location.path, output_path, human_report_path
                )
        except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    report = {
        "schema_version": 1,
        "status": "complete",
        "version": SOURCE_QA_VERSION,
        "input_file": paths.relative(paths.source_segments),
        "input_sha256": input_hash,
        "source_sha256": _sha256_bytes(source_data),
        "prompt_sha256": _sha256_bytes(prompt_data),
        "total_blocks": len(blocks),
        "flagged_blocks": len(flagged_ids),
        "total_issues": len(issues),
        "issue_counts": severity_counts,
        "allowed_tokens": list(allowed_tokens),
        "issues": [issue.to_dict() for issue in issues],
        "updated_at": _utc_now(),
    }
    output_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    _write_bytes_atomic(output_path, output_bytes)
    human_report_bytes = _render_markdown_report(
        total_blocks=len(blocks),
        issues=issues,
        severity_counts=severity_counts,
    )
    _write_bytes_atomic(human_report_path, human_report_bytes)
    state = {key: value for key, value in report.items() if key != "issues"}
    state["output_file"] = paths.relative(paths.source_qa_json)
    state["output_sha256"] = _sha256_bytes(output_bytes)
    state["human_report_file"] = paths.relative(paths.source_qa_markdown)
    state["human_report_sha256"] = _sha256_bytes(human_report_bytes)
    _write_json_atomic(state_path, state)
    return SourceQaResult(
        project_path=str(location.path),
        input_sha256=input_hash,
        total_blocks=len(blocks),
        flagged_blocks=len(flagged_ids),
        total_issues=len(issues),
        error_count=severity_counts["error"],
        warning_count=severity_counts["warning"],
        info_count=severity_counts["info"],
        allowed_tokens=allowed_tokens,
        output_file=str(output_path),
        human_report_file=str(human_report_path),
    )
