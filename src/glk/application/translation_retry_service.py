"""Selectively retranslate translation-review blocks that failed local QA."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application._translation_context import (
    load_approved_blocks as _load_approved_blocks,
    load_termbase as _load_termbase,
    resolve_translation_prompt as _resolve_prompt,
)
from glk.application.project_service import ProjectLocation, load_project
from glk.application.translation_review_service import (
    TranslationReviewConflictError,
    TranslationReviewError,
    get_project_translation_review_document,
    run_project_translation_qa,
    save_project_translation_review,
)
from glk.application.translation_service import (
    compile_translation_prompt,
    validate_translation_response,
)
from glk.application.translation_types import (
    TranslationError,
    TranslationProvider,
    TranslationValidationError,
)
from glk.infrastructure.gemini_translation import GeminiTranslationProvider
from glk.domain.workspace import WorkspacePaths


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class TranslationRetryResult:
    project_path: str
    model: str
    requested_blocks: int
    retried_blocks: int
    block_ids: tuple[str, ...]
    previous_error_count: int
    remaining_error_count: int
    warning_count: int
    review_file: str | None
    revision_file: str | None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.dry_run or self.remaining_error_count == 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["block_ids"] = list(self.block_ids)
        value["ok"] = self.ok
        return value


@dataclass(frozen=True, slots=True)
class _TranslationRetryContext:
    location: ProjectLocation
    paths: WorkspacePaths
    document: Any
    target_blocks: tuple[Any, ...]
    target_ids: tuple[str, ...]
    selected_model: str
    previous_error_count: int


@dataclass(frozen=True, slots=True)
class _TranslationRetryExecution:
    translations: dict[str, str]
    changes: tuple[dict[str, Any], ...]


def _read_translation_model(project_path: Path) -> str | None:
    state_path = WorkspacePaths(project_path).translation_state
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    model = value.get("model") if isinstance(value, dict) else None
    return model if isinstance(model, str) and model.strip() else None


def _revision_path(project_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        WorkspacePaths(project_path).translation_revisions
        / f"translation_retry_{stamp}.json"
    )


def _prepare_translation_retry(
    *,
    project: str | Path,
    workspace_root: str | Path,
    expected_review_sha256: str | None,
    provider: TranslationProvider | None,
    model_name: str | None,
) -> _TranslationRetryContext:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    document = get_project_translation_review_document(
        project=location.path,
        workspace_root=workspace_root,
    )
    if (
        expected_review_sha256 is not None
        and document["review_sha256"] != expected_review_sha256
    ):
        raise TranslationReviewConflictError(
            "The review TXT changed after this page was loaded. Reload before retrying."
        )
    if any(
        issue["severity"] == "error"
        for issue in document["general_issues"]
    ):
        raise TranslationReviewError(
            "The review TXT structure has errors that cannot be retranslated. "
            "Repair or deliberately reset the review first."
        )
    target_blocks = tuple(
        block
        for block in document["blocks"]
        if any(issue["severity"] == "error" for issue in block["issues"])
    )
    selected_model = (
        provider.model_name
        if provider is not None
        else model_name
        or _read_translation_model(location.path)
        or "configured default"
    )
    return _TranslationRetryContext(
        location,
        paths,
        document,
        target_blocks,
        tuple(block["id"] for block in target_blocks),
        selected_model,
        int(document["summary"]["errors"]),
    )


def _unchanged_retry_result(
    context: _TranslationRetryContext,
    *,
    dry_run: bool,
) -> TranslationRetryResult:
    return TranslationRetryResult(
        project_path=str(context.location.path),
        model=context.selected_model,
        requested_blocks=len(context.target_blocks),
        retried_blocks=0,
        block_ids=context.target_ids,
        previous_error_count=context.previous_error_count,
        remaining_error_count=context.previous_error_count,
        warning_count=int(context.document["summary"]["warnings"]),
        review_file=(
            None if dry_run else str(context.paths.translation_review)
        ),
        revision_file=None,
        dry_run=dry_run,
    )


def _retry_translation_block(
    *,
    review_block: Any,
    source_block: Any,
    provider: TranslationProvider,
    termbase_entries: Any,
    project_instructions: str,
    index: int,
    total: int,
    notify: ProgressCallback,
) -> str:
    block_id = review_block["id"]
    qa_feedback = "\n".join(
        f"{issue['code']}: {issue['message']}"
        for issue in review_block["issues"]
        if issue["severity"] == "error"
    )
    translated: dict[str, str] | None = None
    validation_feedback = qa_feedback
    notify(f"오류 블록 {index}/{total} 재번역 중: {block_id}")
    try:
        for attempt in range(2):
            prompt = compile_translation_prompt(
                blocks=(source_block,),
                termbase_entries=termbase_entries,
                project_instructions=project_instructions,
                validation_feedback=validation_feedback,
            )
            try:
                translated = validate_translation_response(
                    response=provider.translate(prompt),
                    blocks=(source_block,),
                    termbase_entries=termbase_entries,
                )
                break
            except TranslationValidationError as error:
                validation_feedback = (
                    f"Original QA errors:\n{qa_feedback}\n"
                    f"Latest response errors:\n{error}"
                )
                notify(
                    f"오류 블록 {index}/{total} 검증 재시도 "
                    f"({attempt + 1}/2)"
                )
    except Exception as error:
        raise TranslationError(
            f"Selective retranslation failed for {block_id}; "
            f"the review was not changed. Cause: {error}"
        ) from error
    if translated is None:
        raise TranslationValidationError(
            f"Selective retranslation failed validation for {block_id}; "
            "the review was not changed."
        )
    return translated[block_id]


def _execute_translation_retry(
    *,
    context: _TranslationRetryContext,
    provider: TranslationProvider,
    approved_by_id: dict[str, Any],
    termbase_entries: Any,
    project_instructions: str,
    notify: ProgressCallback,
) -> _TranslationRetryExecution:
    translations = {
        block["id"]: block["translation"]
        for block in context.document["blocks"]
    }
    changes: list[dict[str, Any]] = []
    total = len(context.target_blocks)
    for index, review_block in enumerate(context.target_blocks, start=1):
        block_id = review_block["id"]
        source_block = approved_by_id[block_id]
        new_text = _retry_translation_block(
            review_block=review_block,
            source_block=source_block,
            provider=provider,
            termbase_entries=termbase_entries,
            project_instructions=project_instructions,
            index=index,
            total=total,
            notify=notify,
        )
        old_text = translations[block_id]
        translations[block_id] = new_text
        changes.append(
            {
                "block_id": block_id,
                "source": source_block.effective_text,
                "previous_translation": old_text,
                "retried_translation": new_text,
                "qa_errors": [
                    issue
                    for issue in review_block["issues"]
                    if issue["severity"] == "error"
                ],
            }
        )
    return _TranslationRetryExecution(translations, tuple(changes))


def _save_translation_retry(
    *,
    context: _TranslationRetryContext,
    execution: _TranslationRetryExecution,
    provider: TranslationProvider,
    workspace_root: str | Path,
) -> TranslationRetryResult:
    saved_document = save_project_translation_review(
        project=context.location.path,
        workspace_root=workspace_root,
        translations=execution.translations,
        expected_review_sha256=context.document["review_sha256"],
    )
    revision_path = _revision_path(context.location.path)
    _write_json_atomic(
        revision_path,
        {
            "schema_version": 1,
            "created_at": (
                datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "project_id": context.document["project"]["id"],
            "model": provider.model_name,
            "provider_prompt_version": provider.prompt_version,
            "review_sha256_before": context.document["review_sha256"],
            "review_sha256_after": saved_document["review_sha256"],
            "retried_blocks": len(execution.changes),
            "changes": list(execution.changes),
        },
    )
    qa_result = run_project_translation_qa(
        project=context.location.path,
        workspace_root=workspace_root,
    )
    return TranslationRetryResult(
        project_path=str(context.location.path),
        model=provider.model_name,
        requested_blocks=len(context.target_blocks),
        retried_blocks=len(execution.changes),
        block_ids=context.target_ids,
        previous_error_count=context.previous_error_count,
        remaining_error_count=qa_result.error_count,
        warning_count=qa_result.warning_count,
        review_file=str(context.paths.translation_review),
        revision_file=str(revision_path),
    )


def retry_failed_translations(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    model_name: str | None = None,
    dry_run: bool = False,
    provider: TranslationProvider | None = None,
    expected_review_sha256: str | None = None,
    progress: ProgressCallback | None = None,
) -> TranslationRetryResult:
    """Retranslate only block-linked QA errors and preserve every other review block."""
    notify = progress or (lambda _: None)
    context = _prepare_translation_retry(
        project=project,
        workspace_root=workspace_root,
        expected_review_sha256=expected_review_sha256,
        provider=provider,
        model_name=model_name,
    )
    if dry_run or not context.target_blocks:
        return _unchanged_retry_result(context, dry_run=dry_run)

    approved_blocks, _ = _load_approved_blocks(context.location.path)
    approved_by_id = {block.id: block for block in approved_blocks}
    missing = [
        block_id
        for block_id in context.target_ids
        if block_id not in approved_by_id
    ]
    if missing:
        raise TranslationError(
            "QA error blocks are missing from approved source: "
            + ", ".join(missing)
        )
    termbase_entries, _ = _load_termbase(context.location.path)
    project_instructions, _, _ = _resolve_prompt(
        None,
        context.location.path,
    )
    active_provider = provider or GeminiTranslationProvider.from_environment(
        model_name or _read_translation_model(context.location.path),
        settings_root=settings_root,
    )
    execution = _execute_translation_retry(
        context=context,
        provider=active_provider,
        approved_by_id=approved_by_id,
        termbase_entries=termbase_entries,
        project_instructions=project_instructions,
        notify=notify,
    )
    return _save_translation_retry(
        context=context,
        execution=execution,
        provider=active_provider,
        workspace_root=workspace_root,
    )
