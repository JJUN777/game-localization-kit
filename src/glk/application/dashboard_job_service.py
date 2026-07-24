"""Run source preparation outside the dashboard HTTP request thread."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable
from uuid import uuid4

from glk.application._io import write_json_atomic
from glk.application.extraction_service import extract_project_pdf
from glk.application.image_ocr_service import ocr_project_images
from glk.application.project_service import load_workspace_project_id
from glk.application.segmentation_service import segment_project_source
from glk.application.source_qa_service import run_project_source_qa
from glk.domain.workspace import (
    IMAGE_SOURCE_ROOT,
    WorkspacePaths,
    is_pdf_source_file,
)


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "interrupted"}
)
_PDF_PROGRESS = re.compile(r"^Page (\d+):")
_IMAGE_PROGRESS = re.compile(r"^Image (\d+)/(\d+):")

JobProgress = Callable[[str, int | None, int | None], None]
SourceJobRunner = Callable[
    [str, str | Path, str, JobProgress],
    dict[str, Any],
]


class DashboardJobError(ValueError):
    """Raised when a dashboard background job cannot be used safely."""


class DashboardJobConflict(DashboardJobError):
    """Raised when a conflicting source job is already active."""


@dataclass(slots=True)
class DashboardSourceJob:
    job_id: str
    project_id: str
    source_type: str
    model: str
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


def _safe_provider_error(errors: list[str], model: str) -> str:
    """Translate provider details into a safe, actionable user message."""
    combined = "\n".join(errors).casefold()
    if any(
        marker in combined
        for marker in (
            "api key not valid",
            "api_key_invalid",
            "invalid api key",
            "unauthenticated",
            "401 unauthorized",
        )
    ):
        return "Gemini API 키가 올바르지 않습니다. AI 설정에서 키를 확인하세요."
    if any(
        marker in combined
        for marker in (
            "resource_exhausted",
            "resource exhausted",
            "quota",
            "rate limit",
            "too many requests",
            "429",
        )
    ):
        return (
            "Gemini API 사용량 한도를 초과했습니다. "
            "사용량 또는 결제 설정을 확인한 뒤 다시 시도하세요."
        )
    if any(
        marker in combined
        for marker in (
            "permission_denied",
            "permission denied",
            "403 forbidden",
            "403 permission",
        )
    ):
        return (
            "Gemini API 호출 권한이 없습니다. "
            "API 키 권한과 Google AI 프로젝트 설정을 확인하세요."
        )
    if any(
        marker in combined
        for marker in (
            "not_found",
            "not found for api version",
            "not supported for generatecontent",
            "model was not found",
            "404 not found",
        )
    ):
        return (
            f"선택한 Gemini 모델 '{model}'을 사용할 수 없습니다. "
            "AI 설정에서 모델을 확인하세요."
        )
    if any(
        marker in combined
        for marker in (
            "timed out",
            "timeout",
            "connection error",
            "connection refused",
            "name resolution",
            "network",
        )
    ):
        return (
            "Gemini API에 연결하지 못했습니다. "
            "네트워크 연결을 확인한 뒤 다시 시도하세요."
        )
    if any(
        marker in combined
        for marker in (
            "empty ocr response",
            "empty layout response",
            "empty response",
        )
    ):
        return "Gemini가 빈 응답을 반환했습니다. 다시 시도하세요."
    if any(
        marker in combined
        for marker in (
            "json",
            "schema",
            "fragment validation",
            "non-object",
        )
    ):
        return "Gemini 응답 형식을 검증하지 못했습니다. 다시 시도하세요."
    return "원본을 처리하지 못했습니다. 원본 파일을 확인한 뒤 다시 시도하세요."


def _acquisition_failure_message(
    acquisition: dict[str, Any],
    *,
    model: str,
    total: int,
    all_failed: bool,
) -> str:
    failures = acquisition.get("failures")
    failure_items = (
        list(failures) if isinstance(failures, (list, tuple)) else []
    )
    errors = [
        str(item.get("error"))
        for item in failure_items
        if isinstance(item, dict) and item.get("error")
    ]
    detail = _safe_provider_error(errors, model)
    if all_failed:
        return detail
    failed_count = len(failure_items)
    if total > 0 and failed_count > 0:
        return f"전체 {total}개 중 {failed_count}개 처리에 실패했습니다. {detail}"
    return f"일부 원본 처리에 실패했습니다. {detail}"


def _registered_source_type(project_id: str, workspace_root: str | Path) -> str:
    location = load_workspace_project_id(project_id, workspace_root)
    if is_pdf_source_file(location.manifest.source_file):
        return "pdf"
    if location.manifest.source_file == IMAGE_SOURCE_ROOT:
        return "images"
    raise DashboardJobError(
        "Project requires one registered PDF or image source."
    )


def run_registered_source_pipeline(
    project_id: str,
    workspace_root: str | Path,
    model: str,
    progress: JobProgress,
) -> dict[str, Any]:
    """Acquire a registered source and prepare local review artifacts."""
    source_type = _registered_source_type(project_id, workspace_root)
    progress("등록된 원본을 확인하고 있습니다.", 0, None)
    if source_type == "pdf":
        planned = extract_project_pdf(
            project=project_id,
            workspace_root=workspace_root,
            model_name=model,
            dry_run=True,
        )
        selected_pages = list(planned.selected_pages)
        total = len(selected_pages)
        page_positions = {
            page: index
            for index, page in enumerate(selected_pages, start=1)
        }

        def report_pdf(message: str) -> None:
            match = _PDF_PROGRESS.match(message)
            current = (
                max(0, page_positions.get(int(match.group(1)), 1) - 1)
                if match
                else None
            )
            progress(message, current, total)

        acquisition = extract_project_pdf(
            project=project_id,
            workspace_root=workspace_root,
            model_name=model,
            progress=report_pdf,
        )
    else:
        planned = ocr_project_images(
            project=project_id,
            workspace_root=workspace_root,
            model_name=model,
            dry_run=True,
        )
        total = len(planned.selected_images)

        def report_image(message: str) -> None:
            match = _IMAGE_PROGRESS.match(message)
            current = max(0, int(match.group(1)) - 1) if match else None
            message_total = int(match.group(2)) if match else total
            progress(message, current, message_total)

        acquisition = ocr_project_images(
            project=project_id,
            workspace_root=workspace_root,
            model_name=model,
            progress=report_image,
        )

    progress("원문 획득 결과를 확인하고 있습니다.", total, total)
    acquisition_data = acquisition.to_dict()
    if not acquisition.ok:
        successful_values = acquisition_data.get(
            "successful_pages"
            if source_type == "pdf"
            else "successful_images"
        )
        successful_count = (
            len(successful_values)
            if isinstance(successful_values, (list, tuple))
            else 0
        )
        all_failed = total > 0 and successful_count == 0
        return {
            "ok": False,
            "status": "failed" if all_failed else "partial",
            "error": _acquisition_failure_message(
                acquisition_data,
                model=model,
                total=total,
                all_failed=all_failed,
            ),
            "source_type": source_type,
            "acquisition": acquisition_data,
            "segmentation": None,
            "qa": None,
        }

    progress("검수용 원문 블록을 생성하고 있습니다.", total, total)
    segmentation = segment_project_source(
        project=project_id,
        workspace_root=workspace_root,
    )
    progress("로컬 원문 QA를 실행하고 있습니다.", total, total)
    qa = run_project_source_qa(
        project=project_id,
        workspace_root=workspace_root,
    )
    progress("원문 검수 준비가 완료되었습니다.", total, total)
    return {
        "ok": True,
        "status": "succeeded",
        "source_type": source_type,
        "acquisition": acquisition_data,
        "segmentation": segmentation.to_dict(),
        "qa": qa.to_dict(),
    }


class DashboardJobManager:
    """Own the single active source job and latest per-project records."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runner: SourceJobRunner = run_registered_source_pipeline,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._runner = runner
        self._lock = threading.RLock()
        self._jobs: dict[str, DashboardSourceJob] = {}
        self._closed = False
        self._load_records()

    def _state_path(self, project_id: str) -> Path:
        location = load_workspace_project_id(
            project_id,
            self.workspace_root,
        )
        return WorkspacePaths(location.path).dashboard_source_job_state

    def _persist(self, job: DashboardSourceJob) -> None:
        write_json_atomic(self._state_path(job.project_id), job.to_dict())

    def _upgrade_acquisition_failure(
        self,
        job: DashboardSourceJob,
    ) -> bool:
        if job.status not in {"partial", "failed"} or not job.result:
            return False
        acquisition = job.result.get("acquisition")
        if not isinstance(acquisition, dict):
            return False
        failures = acquisition.get("failures")
        if not isinstance(failures, list) or not failures:
            return False
        selected_key = (
            "selected_pages" if job.source_type == "pdf" else "selected_images"
        )
        successful_key = (
            "successful_pages"
            if job.source_type == "pdf"
            else "successful_images"
        )
        selected = acquisition.get(selected_key)
        successful = acquisition.get(successful_key)
        if not isinstance(selected, (list, tuple)) or not selected:
            return False
        successful_count = (
            len(successful) if isinstance(successful, (list, tuple)) else 0
        )
        all_failed = successful_count == 0
        status = "failed" if all_failed else "partial"
        error = _acquisition_failure_message(
            acquisition,
            model=job.model,
            total=len(selected),
            all_failed=all_failed,
        )
        progress_message = (
            "원문 준비 작업에 실패했습니다."
            if all_failed
            else "일부 원본 처리에 실패했습니다."
        )
        if (
            job.status == status
            and job.error == error
            and job.progress_message == progress_message
        ):
            return False
        job.status = status
        job.result["status"] = status
        job.result["error"] = error
        job.error = error
        job.progress_message = progress_message
        job.updated_at = _utc_now()
        return True

    def _load_records(self) -> None:
        if not self.workspace_root.is_dir():
            return
        for state_path in self.workspace_root.glob(
            "*/.glk/state/dashboard_source_job.json"
        ):
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                job = DashboardSourceJob(**value)
                changed = False
                if job.status in ACTIVE_JOB_STATUSES:
                    now = _utc_now()
                    job.status = "interrupted"
                    job.progress_message = (
                        "이전 대시보드가 종료되어 작업 상태를 확인할 수 없습니다."
                    )
                    job.error = "Dashboard process stopped before job completion."
                    job.finished_at = now
                    job.updated_at = now
                    changed = True
                if self._upgrade_acquisition_failure(job):
                    changed = True
                if changed:
                    write_json_atomic(state_path, job.to_dict())
                if job.status not in TERMINAL_JOB_STATUSES:
                    continue
                self._jobs[job.project_id] = job
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue

    def _active_job(self) -> DashboardSourceJob | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            ),
            None,
        )

    def is_project_active(self, project_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(project_id)
            return bool(job and job.status in ACTIVE_JOB_STATUSES)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )
            return [job.to_dict() for job in jobs]

    def start_source_job(
        self,
        *,
        project_id: str,
        model: str,
    ) -> dict[str, Any]:
        source_type = _registered_source_type(
            project_id,
            self.workspace_root,
        )
        with self._lock:
            if self._closed:
                raise DashboardJobError("Dashboard job manager is closed.")
            active = self._active_job()
            if active is not None:
                if active.project_id == project_id:
                    raise DashboardJobConflict(
                        "This project already has a source job running."
                    )
                raise DashboardJobConflict(
                    "Another project source job is already running."
                )
            now = _utc_now()
            job = DashboardSourceJob(
                job_id=uuid4().hex,
                project_id=project_id,
                source_type=source_type,
                model=model,
                status="queued",
                progress_message="작업 실행을 준비하고 있습니다.",
                progress_current=None,
                progress_total=None,
                result=None,
                error=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            self._jobs[project_id] = job
            self._persist(job)
            thread = threading.Thread(
                target=self._execute,
                args=(job.job_id, project_id),
                name=f"glk-source-job-{project_id}",
                daemon=True,
            )
            thread.start()
            return job.to_dict()

    def _execute(self, job_id: str, project_id: str) -> None:
        with self._lock:
            job = self._jobs.get(project_id)
            if job is None or job.job_id != job_id:
                return
            now = _utc_now()
            job.status = "running"
            job.started_at = now
            job.updated_at = now
            job.progress_message = "원문 준비 작업을 시작했습니다."
            self._persist(job)

        def report(
            message: str,
            current: int | None,
            total: int | None,
        ) -> None:
            with self._lock:
                current_job = self._jobs.get(project_id)
                if current_job is None or current_job.job_id != job_id:
                    return
                current_job.progress_message = message
                if current is not None:
                    current_job.progress_current = current
                if total is not None:
                    current_job.progress_total = total
                current_job.updated_at = _utc_now()
                self._persist(current_job)

        try:
            result = self._runner(
                project_id,
                self.workspace_root,
                job.model,
                report,
            )
            status = str(result.get("status") or "")
            if status not in {"succeeded", "partial", "failed"}:
                raise DashboardJobError(
                    "Source job runner returned an invalid terminal status."
                )
            error_value = result.get("error")
            error = (
                str(error_value)
                if status in {"partial", "failed"} and error_value
                else (
                    "일부 원본 처리에 실패했습니다. 다시 시도하세요."
                    if status == "partial"
                    else (
                        "원문 준비 작업에 실패했습니다. 다시 시도하세요."
                        if status == "failed"
                        else None
                    )
                )
            )
        except Exception as caught:
            result = None
            status = "failed"
            error = str(caught) or caught.__class__.__name__

        with self._lock:
            current_job = self._jobs.get(project_id)
            if current_job is None or current_job.job_id != job_id:
                return
            now = _utc_now()
            current_job.status = status
            current_job.result = result
            current_job.error = error
            current_job.finished_at = now
            current_job.updated_at = now
            if status == "succeeded":
                current_job.progress_message = "원문 검수 준비가 완료되었습니다."
            elif status == "partial":
                current_job.progress_message = (
                    "일부 원본 처리에 실패했습니다."
                )
            else:
                current_job.progress_message = "원문 준비 작업에 실패했습니다."
            self._persist(current_job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
