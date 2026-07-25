"""Run long-lived dashboard work outside the HTTP request thread."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable
from uuid import uuid4

from glk.application._io import write_bytes_atomic, write_json_atomic
from glk.application.extraction_service import extract_project_pdf
from glk.application.glossary_service import (
    GlossaryBuildError,
    build_project_glossary_candidates,
)
from glk.application.image_ocr_service import ocr_project_images
from glk.application.project_service import (
    inspect_project,
    load_workspace_project_id,
)
from glk.application.segmentation_service import segment_project_source
from glk.application.source_qa_service import run_project_source_qa
from glk.application.translation_prompt_service import (
    MAX_TRANSLATION_PROMPT_BYTES,
)
from glk.application.translation_restart_service import (
    archive_translation_restart,
    clear_stale_translation_review_artifacts,
)
from glk.application.translation_review_service import (
    prepare_project_translation_review,
)
from glk.application.translation_service import translate_project
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
_TRANSLATION_PROGRESS = re.compile(r"^Chunk (\d+)/(\d+):")

JobProgress = Callable[[str, int | None, int | None], None]
SourceJobRunner = Callable[
    [str, str | Path, str, JobProgress],
    dict[str, Any],
]
GlossaryJobRunner = Callable[
    [str, str | Path, JobProgress],
    dict[str, Any],
]
TranslationJobRunner = Callable[
    [str, str | Path, str, bool, bool, JobProgress],
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


@dataclass(slots=True)
class DashboardGlossaryJob:
    job_id: str
    project_id: str
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


@dataclass(slots=True)
class DashboardTranslationJob:
    job_id: str
    project_id: str
    model: str
    resume: bool
    force: bool
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


def _safe_translation_error(error: BaseException, model: str) -> str:
    detail = _safe_provider_error([str(error)], model)
    if detail.startswith("원본을 처리하지 못했습니다."):
        return (
            "초벌 번역에 실패했습니다. 완료된 청크는 보존되었습니다. "
            "다시 시도하면 이어서 진행합니다."
        )
    return detail


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


def run_glossary_pipeline(
    project_id: str,
    workspace_root: str | Path,
    progress: JobProgress,
) -> dict[str, Any]:
    """Build local glossary candidates from the approved source."""
    progress("승인된 원문을 확인하고 있습니다.", 0, 2)
    result = build_project_glossary_candidates(
        project=project_id,
        workspace_root=workspace_root,
    )
    if result.status == "stale":
        raise GlossaryBuildError(
            "기존 용어 검수 파일이 승인 원문과 일치하지 않습니다. "
            "사용자 편집을 보호하기 위해 자동으로 덮어쓰지 않았습니다."
        )
    progress("용어 후보 검수 파일을 생성하고 있습니다.", 1, 2)
    progress("용어 후보 생성이 완료되었습니다.", 2, 2)
    return {
        "ok": True,
        "status": "succeeded",
        "glossary": result.to_dict(),
    }


def run_translation_pipeline(
    project_id: str,
    workspace_root: str | Path,
    model: str,
    resume: bool,
    force: bool,
    progress: JobProgress,
) -> dict[str, Any]:
    """Translate approved source blocks with the current termbase."""
    progress("승인 원문과 용어집을 확인하고 있습니다.", 0, None)
    planned = translate_project(
        project=project_id,
        workspace_root=workspace_root,
        model_name=model,
        resume=resume,
        force=force,
        dry_run=True,
    )
    total = planned.total_chunks
    progress("초벌 번역 청크를 준비하고 있습니다.", 0, total)
    location = (
        load_workspace_project_id(project_id, workspace_root)
        if force
        else None
    )
    revision_path = (
        archive_translation_restart(location)
        if location is not None
        else None
    )

    def report_translation(message: str) -> None:
        match = _TRANSLATION_PROGRESS.match(message)
        current = max(0, int(match.group(1)) - 1) if match else None
        message_total = int(match.group(2)) if match else total
        progress(message, current, message_total)

    result = translate_project(
        project=project_id,
        workspace_root=workspace_root,
        model_name=model,
        resume=resume,
        force=force,
        progress=report_translation,
    )
    review_reset = False
    if location is not None:
        clear_stale_translation_review_artifacts(location)
        prepare_project_translation_review(
            project=location.path,
            workspace_root=workspace_root,
            force=True,
        )
        review_reset = True
    progress("초벌 번역과 검수 파일 생성이 완료되었습니다.", total, total)
    return {
        "ok": True,
        "status": "succeeded",
        "translation": result.to_dict(),
        "revision_path": (
            WorkspacePaths(location.path).relative(revision_path)
            if location is not None and revision_path is not None
            else None
        ),
        "review_reset": review_reset,
    }


class DashboardJobManager:
    """Own one active dashboard job and latest per-project records."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runner: SourceJobRunner | None = None,
        glossary_runner: GlossaryJobRunner | None = None,
        translation_runner: TranslationJobRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._source_runner = runner or run_registered_source_pipeline
        self._glossary_runner = glossary_runner or run_glossary_pipeline
        self._translation_runner = (
            translation_runner or run_translation_pipeline
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, DashboardSourceJob] = {}
        self._glossary_jobs: dict[str, DashboardGlossaryJob] = {}
        self._translation_jobs: dict[str, DashboardTranslationJob] = {}
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

    def _glossary_state_path(self, project_id: str) -> Path:
        location = load_workspace_project_id(
            project_id,
            self.workspace_root,
        )
        return WorkspacePaths(location.path).dashboard_glossary_job_state

    def _persist_glossary(self, job: DashboardGlossaryJob) -> None:
        write_json_atomic(
            self._glossary_state_path(job.project_id),
            job.to_dict(),
        )

    def _translation_state_path(self, project_id: str) -> Path:
        location = load_workspace_project_id(
            project_id,
            self.workspace_root,
        )
        return WorkspacePaths(
            location.path
        ).dashboard_translation_job_state

    def _persist_translation(self, job: DashboardTranslationJob) -> None:
        write_json_atomic(
            self._translation_state_path(job.project_id),
            job.to_dict(),
        )

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
                source_job = DashboardSourceJob(**value)
                changed = False
                if source_job.status in ACTIVE_JOB_STATUSES:
                    now = _utc_now()
                    source_job.status = "interrupted"
                    source_job.progress_message = (
                        "이전 대시보드가 종료되어 작업 상태를 확인할 수 없습니다."
                    )
                    source_job.error = (
                        "Dashboard process stopped before job completion."
                    )
                    source_job.finished_at = now
                    source_job.updated_at = now
                    changed = True
                if self._upgrade_acquisition_failure(source_job):
                    changed = True
                if changed:
                    write_json_atomic(state_path, source_job.to_dict())
                if source_job.status not in TERMINAL_JOB_STATUSES:
                    continue
                self._jobs[source_job.project_id] = source_job
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue
        for state_path in self.workspace_root.glob(
            "*/.glk/state/dashboard_glossary_job.json"
        ):
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                glossary_job = DashboardGlossaryJob(**value)
                if glossary_job.status in ACTIVE_JOB_STATUSES:
                    now = _utc_now()
                    glossary_job.status = "interrupted"
                    glossary_job.progress_message = (
                        "이전 대시보드가 종료되어 작업 상태를 확인할 수 없습니다."
                    )
                    glossary_job.error = (
                        "Dashboard process stopped before job completion."
                    )
                    glossary_job.finished_at = now
                    glossary_job.updated_at = now
                    write_json_atomic(state_path, glossary_job.to_dict())
                if glossary_job.status not in TERMINAL_JOB_STATUSES:
                    continue
                self._glossary_jobs[glossary_job.project_id] = glossary_job
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue
        for state_path in self.workspace_root.glob(
            "*/.glk/state/dashboard_translation_job.json"
        ):
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    value.setdefault("force", False)
                translation_job = DashboardTranslationJob(**value)
                if translation_job.status in ACTIVE_JOB_STATUSES:
                    now = _utc_now()
                    translation_job.status = "interrupted"
                    translation_job.progress_message = (
                        "이전 대시보드가 종료되어 작업 상태를 확인할 수 없습니다."
                    )
                    translation_job.error = (
                        "Dashboard process stopped before job completion."
                    )
                    translation_job.finished_at = now
                    translation_job.updated_at = now
                    write_json_atomic(
                        state_path,
                        translation_job.to_dict(),
                    )
                if translation_job.status not in TERMINAL_JOB_STATUSES:
                    continue
                self._translation_jobs[
                    translation_job.project_id
                ] = translation_job
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue

    def _active_job(
        self,
    ) -> (
        DashboardSourceJob
        | DashboardGlossaryJob
        | DashboardTranslationJob
        | None
    ):
        source_job = next(
            (
                job
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            ),
            None,
        )
        if source_job is not None:
            return source_job
        glossary_job = next(
            (
                job
                for job in self._glossary_jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            ),
            None,
        )
        if glossary_job is not None:
            return glossary_job
        return next(
            (
                job
                for job in self._translation_jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            ),
            None,
        )

    def is_project_active(self, project_id: str) -> bool:
        with self._lock:
            jobs = (
                self._jobs.get(project_id),
                self._glossary_jobs.get(project_id),
                self._translation_jobs.get(project_id),
            )
            return any(
                job is not None and job.status in ACTIVE_JOB_STATUSES
                for job in jobs
            )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )
            return [job.to_dict() for job in jobs]

    def list_glossary_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._glossary_jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )
            return [job.to_dict() for job in jobs]

    def list_translation_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._translation_jobs.values(),
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
                        "This project already has a background job running."
                    )
                raise DashboardJobConflict(
                    "Another project background job is already running."
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
            queued_job = job.to_dict()
            thread = threading.Thread(
                target=self._execute_source,
                args=(job.job_id, project_id),
                name=f"glk-source-job-{project_id}",
                daemon=True,
            )
            thread.start()
            return queued_job

    def start_glossary_job(
        self,
        *,
        project_id: str,
    ) -> dict[str, Any]:
        location = load_workspace_project_id(
            project_id,
            self.workspace_root,
        )
        pipeline = inspect_project(location.path)["pipeline"]
        if pipeline["human_review"] != "approved":
            raise DashboardJobError(
                "Approve the current source before building glossary candidates."
            )
        if pipeline["glossary_status"] == "current":
            raise DashboardJobError(
                "Glossary candidates are already current."
            )
        if pipeline["glossary_status"] == "stale":
            raise DashboardJobError(
                "Existing glossary edits are stale and cannot be overwritten "
                "from the dashboard."
            )
        with self._lock:
            if self._closed:
                raise DashboardJobError("Dashboard job manager is closed.")
            active = self._active_job()
            if active is not None:
                if active.project_id == project_id:
                    raise DashboardJobConflict(
                        "This project already has a background job running."
                    )
                raise DashboardJobConflict(
                    "Another project background job is already running."
                )
            now = _utc_now()
            job = DashboardGlossaryJob(
                job_id=uuid4().hex,
                project_id=project_id,
                status="queued",
                progress_message="용어 후보 생성을 준비하고 있습니다.",
                progress_current=0,
                progress_total=2,
                result=None,
                error=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            self._glossary_jobs[project_id] = job
            self._persist_glossary(job)
            queued_job = job.to_dict()
            thread = threading.Thread(
                target=self._execute_glossary,
                args=(job.job_id, project_id),
                name=f"glk-glossary-job-{project_id}",
                daemon=True,
            )
            thread.start()
            return queued_job

    def start_translation_job(
        self,
        *,
        project_id: str,
        model: str,
        prompt: str,
        force: bool = False,
    ) -> dict[str, Any]:
        prompt_data = prompt.encode("utf-8")
        if not prompt.strip():
            raise DashboardJobError("Translation prompt must not be empty.")
        if len(prompt_data) > MAX_TRANSLATION_PROMPT_BYTES:
            raise DashboardJobError(
                "Translation prompt is too large."
            )
        location = load_workspace_project_id(
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
                        "This project already has a background job running."
                    )
                raise DashboardJobConflict(
                    "Another project background job is already running."
                )
            pipeline = inspect_project(location.path)["pipeline"]
            if not pipeline["final_source_approved"]:
                raise DashboardJobError(
                    "Approve the current source before starting translation."
                )
            if pipeline["termbase_status"] != "current":
                raise DashboardJobError(
                    "Complete the current termbase before starting translation."
                )
            translation_status = pipeline["translation_status"]
            if translation_status == "current" and not force:
                raise DashboardJobError(
                    "Translation draft is already current."
                )
            if translation_status == "stale" and not force:
                raise DashboardJobError(
                    "Existing translation files are stale and cannot be "
                    "overwritten without an explicit full restart."
                )
            if translation_status not in {
                "not_run",
                "partial",
                "current",
                "stale",
            }:
                raise DashboardJobError(
                    "Translation is not ready to start."
                )
            if force and translation_status == "not_run":
                raise DashboardJobError(
                    "A full restart requires an existing translation."
                )
            paths = WorkspacePaths(location.path)
            resume = translation_status == "partial" and not force
            if resume:
                if not paths.translation_prompt.is_file():
                    raise DashboardJobError(
                        "The saved translation prompt is missing."
                    )
                try:
                    saved_prompt = paths.translation_prompt.read_text(
                        encoding="utf-8"
                    )
                except UnicodeDecodeError as error:
                    raise DashboardJobError(
                        "The saved translation prompt must be UTF-8."
                    ) from error
                if saved_prompt != prompt:
                    raise DashboardJobError(
                        "A partial translation must resume with its saved prompt."
                    )
            else:
                if paths.translation_prompt.is_file():
                    try:
                        saved_prompt = paths.translation_prompt.read_text(
                            encoding="utf-8"
                        )
                    except UnicodeDecodeError as error:
                        raise DashboardJobError(
                            "The saved translation prompt must be UTF-8."
                        ) from error
                    if saved_prompt != prompt:
                        raise DashboardJobError(
                            "Save the translation prompt before starting translation."
                        )
                else:
                    write_bytes_atomic(paths.translation_prompt, prompt_data)
            now = _utc_now()
            job = DashboardTranslationJob(
                job_id=uuid4().hex,
                project_id=project_id,
                model=model,
                resume=resume,
                force=force,
                status="queued",
                progress_message="초벌 번역 실행을 준비하고 있습니다.",
                progress_current=0,
                progress_total=None,
                result=None,
                error=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            self._translation_jobs[project_id] = job
            self._persist_translation(job)
            queued_job = job.to_dict()
            thread = threading.Thread(
                target=self._execute_translation,
                args=(job.job_id, project_id),
                name=f"glk-translation-job-{project_id}",
                daemon=True,
            )
            thread.start()
            return queued_job

    def _execute_source(self, job_id: str, project_id: str) -> None:
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
            result = self._source_runner(
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

    def _execute_glossary(self, job_id: str, project_id: str) -> None:
        with self._lock:
            job = self._glossary_jobs.get(project_id)
            if job is None or job.job_id != job_id:
                return
            now = _utc_now()
            job.status = "running"
            job.started_at = now
            job.updated_at = now
            job.progress_message = "용어 후보 생성을 시작했습니다."
            self._persist_glossary(job)

        def report(
            message: str,
            current: int | None,
            total: int | None,
        ) -> None:
            with self._lock:
                current_job = self._glossary_jobs.get(project_id)
                if current_job is None or current_job.job_id != job_id:
                    return
                current_job.progress_message = message
                if current is not None:
                    current_job.progress_current = current
                if total is not None:
                    current_job.progress_total = total
                current_job.updated_at = _utc_now()
                self._persist_glossary(current_job)

        try:
            result = self._glossary_runner(
                project_id,
                self.workspace_root,
                report,
            )
            status = str(result.get("status") or "")
            if status not in {"succeeded", "failed"}:
                raise DashboardJobError(
                    "Glossary job runner returned an invalid terminal status."
                )
            error_value = result.get("error")
            error = (
                str(error_value)
                if status == "failed" and error_value
                else (
                    "용어 후보 생성에 실패했습니다. 다시 시도하세요."
                    if status == "failed"
                    else None
                )
            )
        except Exception as caught:
            result = None
            status = "failed"
            error = str(caught) or caught.__class__.__name__

        with self._lock:
            current_job = self._glossary_jobs.get(project_id)
            if current_job is None or current_job.job_id != job_id:
                return
            now = _utc_now()
            current_job.status = status
            current_job.result = result
            current_job.error = error
            current_job.finished_at = now
            current_job.updated_at = now
            if status == "succeeded":
                current_job.progress_message = "용어 후보 생성이 완료되었습니다."
                current_job.progress_current = current_job.progress_total
            else:
                current_job.progress_message = "용어 후보 생성에 실패했습니다."
            self._persist_glossary(current_job)

    def _execute_translation(
        self,
        job_id: str,
        project_id: str,
    ) -> None:
        with self._lock:
            job = self._translation_jobs.get(project_id)
            if job is None or job.job_id != job_id:
                return
            now = _utc_now()
            job.status = "running"
            job.started_at = now
            job.updated_at = now
            job.progress_message = "초벌 번역을 시작했습니다."
            self._persist_translation(job)

        def report(
            message: str,
            current: int | None,
            total: int | None,
        ) -> None:
            with self._lock:
                current_job = self._translation_jobs.get(project_id)
                if current_job is None or current_job.job_id != job_id:
                    return
                current_job.progress_message = message
                if current is not None:
                    current_job.progress_current = current
                if total is not None:
                    current_job.progress_total = total
                current_job.updated_at = _utc_now()
                self._persist_translation(current_job)

        try:
            result = self._translation_runner(
                project_id,
                self.workspace_root,
                job.model,
                job.resume,
                job.force,
                report,
            )
            status = str(result.get("status") or "")
            if status not in {"succeeded", "failed"}:
                raise DashboardJobError(
                    "Translation job runner returned an invalid terminal status."
                )
            error_value = result.get("error")
            error = (
                str(error_value)
                if status == "failed" and error_value
                else (
                    "초벌 번역에 실패했습니다. 다시 시도하세요."
                    if status == "failed"
                    else None
                )
            )
        except Exception as caught:
            result = None
            status = "failed"
            error = _safe_translation_error(caught, job.model)

        with self._lock:
            current_job = self._translation_jobs.get(project_id)
            if current_job is None or current_job.job_id != job_id:
                return
            now = _utc_now()
            current_job.status = status
            current_job.result = result
            current_job.error = error
            current_job.finished_at = now
            current_job.updated_at = now
            if status == "succeeded":
                current_job.progress_message = (
                    "초벌 번역과 검수 파일 생성이 완료되었습니다."
                )
                current_job.progress_current = current_job.progress_total
            else:
                current_job.progress_message = "초벌 번역에 실패했습니다."
            self._persist_translation(current_job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
