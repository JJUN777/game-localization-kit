"""Store project translation instructions independently from translation runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from glk.application._hashing import normalize_text_newlines, sha256_text
from glk.application._io import write_json_atomic, write_text_atomic
from glk.application.project_service import (
    ProjectLocation,
    inspect_project,
    load_project,
)
from glk.application.translation_types import DEFAULT_PROJECT_INSTRUCTIONS
from glk.domain.workspace import WorkspacePaths


MAX_TRANSLATION_PROMPT_BYTES = 64 * 1024


class TranslationPromptError(ValueError):
    """Raised when project translation instructions cannot be saved safely."""


@dataclass(frozen=True, slots=True)
class TranslationPromptDocument:
    value: str
    saved: bool
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranslationPromptSaveResult:
    project_path: str
    prompt_file: str
    sha256: str
    changed: bool
    translation_status_before: str
    translation_invalidated: bool
    revision_file: str | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = True
        return value


def _canonical_prompt(value: str) -> str:
    if not isinstance(value, str):
        raise TranslationPromptError("번역 프롬프트는 문자열이어야 합니다.")
    if not value.strip():
        raise TranslationPromptError("번역 프롬프트를 입력하세요.")
    if "\x00" in value:
        raise TranslationPromptError("번역 프롬프트에 null 문자를 넣을 수 없습니다.")
    normalized = normalize_text_newlines(value)
    canonical = normalized if normalized.endswith("\n") else normalized + "\n"
    if len(canonical.encode("utf-8")) > MAX_TRANSLATION_PROMPT_BYTES:
        raise TranslationPromptError("번역 프롬프트는 64 KiB 이하여야 합니다.")
    return canonical


def load_translation_prompt_document(
    project_path: Path,
) -> TranslationPromptDocument:
    """Read the saved prompt or return the editable program default."""
    prompt_path = WorkspacePaths(project_path).translation_prompt
    if not prompt_path.is_file():
        value = _canonical_prompt(DEFAULT_PROJECT_INSTRUCTIONS)
        return TranslationPromptDocument(
            value=value,
            saved=False,
            sha256=sha256_text(value),
        )
    try:
        value = prompt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise TranslationPromptError(
            "저장된 번역 프롬프트가 UTF-8 형식이 아닙니다."
        ) from error
    return TranslationPromptDocument(
        value=value,
        saved=True,
        sha256=sha256_text(value),
    )


def _prompt_revision_path(project_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        WorkspacePaths(project_path).translation_revisions
        / f"translation_prompt_change_{stamp}.json"
    )


def save_project_translation_prompt(
    location: ProjectLocation,
    value: str,
    *,
    expected_sha256: str | None = None,
) -> TranslationPromptSaveResult:
    """Save only project instructions and preserve invalidated prompt history."""
    current = load_translation_prompt_document(location.path)
    if expected_sha256 is not None and expected_sha256 != current.sha256:
        raise TranslationPromptError(
            "다른 화면에서 번역 프롬프트가 변경되었습니다. 새로고침 후 다시 시도하세요."
        )
    canonical = _canonical_prompt(value)
    prompt_data = canonical.encode("utf-8")
    changed = prompt_data != current.value.encode("utf-8")
    pipeline = inspect_project(location.path)["pipeline"]
    status_before = str(pipeline["translation_status"])
    invalidated = changed and status_before in {"partial", "current", "stale"}
    revision_path: Path | None = None
    if invalidated:
        revision_path = _prompt_revision_path(location.path)
        write_json_atomic(
            revision_path,
            {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "project_id": location.manifest.project_id,
                "translation_status_before": status_before,
                "previous_prompt": current.value,
                "previous_prompt_sha256": current.sha256,
                "new_prompt": canonical,
                "new_prompt_sha256": sha256_text(canonical),
            },
        )
    prompt_path = WorkspacePaths(location.path).translation_prompt
    write_text_atomic(prompt_path, canonical)
    return TranslationPromptSaveResult(
        project_path=str(location.path),
        prompt_file=WorkspacePaths(location.path).relative(prompt_path),
        sha256=sha256_text(canonical),
        changed=changed,
        translation_status_before=status_before,
        translation_invalidated=invalidated,
        revision_file=(
            WorkspacePaths(location.path).relative(revision_path)
            if revision_path is not None
            else None
        ),
    )


def update_project_translation_prompt(
    *,
    project: str | Path,
    translation_prompt: str,
    workspace_root: str | Path = "workspaces",
    expected_sha256: str | None = None,
) -> TranslationPromptSaveResult:
    """Resolve a project and update only its translation instructions."""
    return save_project_translation_prompt(
        load_project(project, workspace_root),
        translation_prompt,
        expected_sha256=expected_sha256,
    )
