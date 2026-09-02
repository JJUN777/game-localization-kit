"""Run selective translation retries outside the review HTTP request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

from glk.application.translation_review_service import (
    TranslationReviewConflictError,
)
from glk.application.translation_retry_service import (
    TranslationRetryResult,
    retry_failed_translations,
)
from glk.application.translation_types import TranslationValidationError
from glk.config import resolve_settings_root
from glk.infrastructure.ai_provider import ai_failure_code


ACTIVE_RETRY_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_RETRY_JOB_STATUSES = frozenset({"succeeded", "failed"})
_PROGRESS_FRACTION = re.compile(r"(\d+)/(\d+)")

RetryProgress = Callable[[str], None]
TranslationRetryJobRunner = Callable[
    [str | Path, str | Path, str, RetryProgress],
    TranslationRetryResult,
]


class TranslationRetryJobError(ValueError):
    """Raised when a translation retry job cannot be started."""

    code = "TRANSLATION_RETRY_FAILED"


class TranslationRetryJobConflict(TranslationRetryJobError):
    """Raised when another translation retry is already active."""

    code = "TRANSLATION_RETRY_CONFLICT"


@dataclass(slots=True)
class TranslationRetryJob:
    job_id: str
    status: str
    progress_message: str
    progress_current: int | None
    progress_total: int | None
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _run_retry(
    project: str | Path,
    workspace_root: str | Path,
    expected_review_sha256: str,
    progress: RetryProgress,
    *,
    settings_root: str | Path | None = None,
) -> TranslationRetryResult:
    return retry_failed_translations(
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        expected_review_sha256=expected_review_sha256,
        progress=progress,
    )


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _safe_retry_error(error: BaseException) -> str:
    chain = _exception_chain(error)
    if any(isinstance(item, TranslationReviewConflictError) for item in chain):
        return (
            "재번역 중 검수 내용이 변경되었습니다. "
            "최신 내용을 불러온 뒤 다시 시도하세요."
        )
    if any(isinstance(item, TranslationValidationError) for item in chain):
        return (
            "AI 재번역 결과가 검증 규칙을 통과하지 못했습니다. "
            "검수 내용은 유지되었습니다. 직접 수정하거나 다시 시도하세요."
        )
    code = ai_failure_code(error)
    if code == "GEMINI_API_KEY_MISSING":
        return (
            "Gemini API 키가 설정되지 않았습니다. "
            "대시보드의 AI 설정에서 키를 저장한 뒤 다시 시도하세요."
        )
    if code == "OPENAI_API_KEY_MISSING":
        return (
            "OpenAI API 키가 설정되지 않았습니다. "
            "대시보드의 AI 설정에서 키를 저장한 뒤 다시 시도하세요."
        )
    provider = "OpenAI" if code.startswith("OPENAI_") else "Gemini"
    if code.endswith("API_KEY_OR_REQUEST_INVALID"):
        return (
            f"{provider} API 키 또는 요청 설정이 올바르지 않습니다. "
            "AI 설정을 확인한 뒤 다시 시도하세요."
        )
    if code.endswith("PERMISSION_DENIED"):
        return f"{provider} API 호출 권한이 없습니다. API 키와 프로젝트 권한을 확인하세요."
    if code.endswith("MODEL_NOT_FOUND"):
        return f"선택한 {provider} 모델을 사용할 수 없습니다. AI 설정에서 모델을 확인하세요."
    if code.endswith("QUOTA_EXCEEDED"):
        return (
            f"{provider} API 사용량 한도를 초과했습니다. "
            "사용량 또는 결제 설정을 확인한 뒤 다시 시도하세요."
        )
    if code.endswith("TEMPORARILY_UNAVAILABLE"):
        return f"{provider} API가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도하세요."
    if code.endswith("NETWORK_ERROR"):
        return (
            f"{provider} API에 연결하지 못했습니다. "
            "네트워크 연결을 확인한 뒤 다시 시도하세요."
        )
    return (
        "오류 문장 재번역에 실패했습니다. "
        "검수 내용은 유지되었습니다. 다시 시도하세요."
    )


def _exception_usage(error: BaseException) -> dict[str, Any] | None:
    for item in _exception_chain(error):
        usage = getattr(item, "ai_usage", None)
        if isinstance(usage, dict):
            return usage
    return None


def _failed_block(error: BaseException) -> str | None:
    for item in _exception_chain(error):
        match = re.search(r"(?:failed for|validation for) ([^;]+)", str(item))
        if match:
            return match.group(1)
    return None


class TranslationRetryJobManager:
    """Own the latest selective-retranslation job for one review server."""

    def __init__(
        self,
        *,
        project: str | Path,
        workspace_root: str | Path,
        settings_root: str | Path | None = None,
        runner: TranslationRetryJobRunner | None = None,
    ) -> None:
        self.project = project
        self.workspace_root = workspace_root
        self.settings_root = resolve_settings_root(settings_root)
        self._runner = runner
        self._lock = threading.RLock()
        self._job: TranslationRetryJob | None = None
        self._closed = False

    def get_job(self) -> dict[str, Any] | None:
        with self._lock:
            return self._job.to_dict() if self._job is not None else None

    def is_active(self) -> bool:
        with self._lock:
            return (
                self._job is not None
                and self._job.status in ACTIVE_RETRY_JOB_STATUSES
            )

    def start(self, *, expected_review_sha256: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise TranslationRetryJobError(
                    "번역 검수 서버가 종료되어 재번역을 시작할 수 없습니다."
                )
            if self.is_active():
                raise TranslationRetryJobConflict(
                    "오류 문장 재번역이 이미 진행 중입니다."
                )
            now = _utc_now()
            job = TranslationRetryJob(
                job_id=uuid4().hex,
                status="queued",
                progress_message="오류 문장 재번역을 준비하고 있습니다.",
                progress_current=0,
                progress_total=None,
                result=None,
                error=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            self._job = job
            queued = job.to_dict()
            thread = threading.Thread(
                target=self._execute,
                args=(job.job_id, expected_review_sha256),
                name=f"glk-translation-retry-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return queued

    def _execute(
        self,
        job_id: str,
        expected_review_sha256: str,
    ) -> None:
        with self._lock:
            job = self._job
            if job is None or job.job_id != job_id:
                return
            now = _utc_now()
            job.status = "running"
            job.progress_message = "오류 문장 재번역을 시작했습니다."
            job.started_at = now
            job.updated_at = now

        def report(message: str) -> None:
            with self._lock:
                current_job = self._job
                if current_job is None or current_job.job_id != job_id:
                    return
                match = _PROGRESS_FRACTION.search(message)
                if match is not None:
                    current_job.progress_current = max(
                        0, int(match.group(1)) - 1
                    )
                    current_job.progress_total = int(match.group(2))
                current_job.progress_message = message
                current_job.updated_at = _utc_now()

        result_payload: dict[str, Any] | None
        error: str | None
        try:
            if self._runner is not None:
                result = self._runner(
                    self.project,
                    self.workspace_root,
                    expected_review_sha256,
                    report,
                )
            else:
                result = _run_retry(
                    self.project,
                    self.workspace_root,
                    expected_review_sha256,
                    report,
                    settings_root=self.settings_root,
                )
        except Exception as caught:
            status = "failed"
            error = _safe_retry_error(caught)
            failed_block = _failed_block(caught)
            result_payload = {
                "usage": _exception_usage(caught),
                "failure_details": [
                    {
                        "item": failed_block or "재번역 블록",
                        "message": error,
                    }
                ],
            }
        else:
            result_payload = result.to_dict()
            status = "succeeded"
            error = None

        with self._lock:
            current_job = self._job
            if current_job is None or current_job.job_id != job_id:
                return
            now = _utc_now()
            current_job.status = status
            current_job.result = result_payload
            current_job.error = error
            current_job.finished_at = now
            current_job.updated_at = now
            if status == "succeeded":
                current_job.progress_message = "오류 문장 재번역이 완료되었습니다."
                current_job.progress_current = current_job.progress_total
            else:
                current_job.progress_message = "오류 문장 재번역에 실패했습니다."

    def close(self) -> None:
        with self._lock:
            self._closed = True
