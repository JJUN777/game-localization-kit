"""Run long-lived dashboard work outside the HTTP request thread."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
from typing import Any, Callable, Generic, TypeVar
from uuid import uuid4

from glk.application._cache import read_json_object
from glk.application._io import write_bytes_atomic, write_json_atomic
from glk.application.extraction_service import ExtractionResult, extract_project_pdf
from glk.application.glossary_service import (
    GlossaryBuildError,
    GlossaryReviewStaleError,
    build_project_glossary_candidates,
)
from glk.application.image_ocr_service import ImageOcrRunResult, ocr_project_images
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
    run_project_translation_qa,
)
from glk.application.translation_service import translate_project
from glk.application.translation_types import TranslationValidationError
from glk.config import resolve_settings_root
from glk.domain.workspace import (
    IMAGE_SOURCE_ROOT,
    WorkspacePaths,
    is_pdf_source_file,
)
from glk.infrastructure.ai_provider import ai_failure_code


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "interrupted"}
)
JOB_SCHEMA_VERSION = 1
_JOB_STATUSES = ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES
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


_COMMON_JOB_FIELDS = frozenset(
    {
        "job_id",
        "project_id",
        "status",
        "progress_message",
        "progress_current",
        "progress_total",
        "result",
        "error",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
    }
)


def _validated_job_payload(
    value: dict[str, Any],
    *,
    expected_project_id: str,
    extra_fields: frozenset[str],
) -> dict[str, Any]:
    fields = _COMMON_JOB_FIELDS | extra_fields
    schema_version = value.get("schema_version", JOB_SCHEMA_VERSION)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != JOB_SCHEMA_VERSION
    ):
        raise DashboardJobError(
            f"Unsupported dashboard job schema version: {schema_version!r}"
        )
    unknown = set(value) - fields - {"schema_version"}
    missing = fields - set(value)
    if missing:
        raise DashboardJobError(
            "Dashboard job state is missing fields: "
            + ", ".join(sorted(missing))
        )
    if unknown:
        raise DashboardJobError(
            "Dashboard job state has unknown fields: "
            + ", ".join(sorted(unknown))
        )

    payload = {name: value[name] for name in fields}
    for name in ("job_id", "project_id", "status", "progress_message"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise DashboardJobError(
                f"Dashboard job field {name} must be a non-empty string."
            )
    if payload["status"] not in _JOB_STATUSES:
        raise DashboardJobError(
            f"Dashboard job status is invalid: {payload['status']!r}"
        )
    for name in ("progress_current", "progress_total"):
        progress = payload[name]
        if progress is not None and (
            not isinstance(progress, int)
            or isinstance(progress, bool)
            or progress < 0
        ):
            raise DashboardJobError(
                f"Dashboard job field {name} must be a non-negative integer or null."
            )
    current = payload["progress_current"]
    total = payload["progress_total"]
    if current is not None and total is not None and current > total:
        raise DashboardJobError(
            "Dashboard job progress_current must not exceed progress_total."
        )
    if payload["result"] is not None and not isinstance(payload["result"], dict):
        raise DashboardJobError(
            "Dashboard job result must be an object or null."
        )
    if payload["error"] is not None and not isinstance(payload["error"], str):
        raise DashboardJobError(
            "Dashboard job error must be a string or null."
        )
    for name in ("created_at", "updated_at"):
        _validate_job_timestamp(payload[name], name, optional=False)
    for name in ("started_at", "finished_at"):
        _validate_job_timestamp(payload[name], name, optional=True)
    payload["project_id"] = expected_project_id
    return payload


def _validate_job_timestamp(
    value: Any,
    name: str,
    *,
    optional: bool,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value:
        raise DashboardJobError(
            f"Dashboard job field {name} must be an ISO-8601 timestamp"
            + (" or null." if optional else ".")
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DashboardJobError(
            f"Dashboard job field {name} is not a valid ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise DashboardJobError(
            f"Dashboard job field {name} must include a timezone."
        )


@dataclass(slots=True)
class DashboardJobRecord:
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
        return {"schema_version": JOB_SCHEMA_VERSION, **asdict(self)}


@dataclass(slots=True)
class DashboardSourceJob(DashboardJobRecord):
    source_type: str
    model: str

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        expected_project_id: str,
    ) -> DashboardSourceJob:
        payload = _validated_job_payload(
            value,
            expected_project_id=expected_project_id,
            extra_fields=frozenset({"source_type", "model"}),
        )
        if payload["source_type"] not in {"pdf", "images"}:
            raise DashboardJobError(
                "Dashboard source job source_type must be pdf or images."
            )
        if not isinstance(payload["model"], str) or not payload["model"]:
            raise DashboardJobError(
                "Dashboard source job model must be a non-empty string."
            )
        return cls(**payload)


@dataclass(slots=True)
class DashboardGlossaryJob(DashboardJobRecord):
    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        expected_project_id: str,
    ) -> DashboardGlossaryJob:
        return cls(
            **_validated_job_payload(
                value,
                expected_project_id=expected_project_id,
                extra_fields=frozenset(),
            )
        )


@dataclass(slots=True)
class DashboardTranslationJob(DashboardJobRecord):
    model: str
    resume: bool
    force: bool

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        expected_project_id: str,
    ) -> DashboardTranslationJob:
        legacy = dict(value)
        legacy.setdefault("force", False)
        payload = _validated_job_payload(
            legacy,
            expected_project_id=expected_project_id,
            extra_fields=frozenset({"model", "resume", "force"}),
        )
        if not isinstance(payload["model"], str) or not payload["model"]:
            raise DashboardJobError(
                "Dashboard translation job model must be a non-empty string."
            )
        for name in ("resume", "force"):
            if not isinstance(payload[name], bool):
                raise DashboardJobError(
                    f"Dashboard translation job {name} must be a boolean."
                )
        return cls(**payload)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _safe_provider_error(codes: list[str], model: str) -> str:
    """Translate stable provider failure codes into actionable guidance."""
    code_set = set(codes)
    if "AI_RESPONSE_INVALID" in code_set:
        return "AI 응답 형식을 검증하지 못했습니다. 다시 시도하세요."
    if "GEMINI_API_KEY_MISSING" in code_set:
        return (
            "Gemini API 키가 설정되지 않았습니다. "
            "대시보드의 AI 설정에서 키를 저장한 뒤 다시 시도하세요."
        )
    if "GEMINI_API_KEY_OR_REQUEST_INVALID" in code_set:
        return "Gemini API 키가 올바르지 않습니다. AI 설정에서 키를 확인하세요."
    if "GEMINI_QUOTA_EXCEEDED" in code_set:
        return (
            "Gemini API 사용량 한도를 초과했습니다. "
            "사용량 또는 결제 설정을 확인한 뒤 다시 시도하세요."
        )
    if "GEMINI_PERMISSION_DENIED" in code_set:
        return (
            "Gemini API 호출 권한이 없습니다. "
            "API 키 권한과 Google AI 프로젝트 설정을 확인하세요."
        )
    if "GEMINI_MODEL_NOT_FOUND" in code_set:
        return (
            f"선택한 Gemini 모델 '{model}'을 사용할 수 없습니다. "
            "AI 설정에서 모델을 확인하세요."
        )
    if "GEMINI_NETWORK_ERROR" in code_set:
        return (
            "Gemini API에 연결하지 못했습니다. "
            "네트워크 연결을 확인한 뒤 다시 시도하세요."
        )
    if "GEMINI_TEMPORARILY_UNAVAILABLE" in code_set:
        return (
            "Gemini API가 일시적으로 응답하지 않습니다. "
            "잠시 후 다시 시도하세요."
        )
    if "GEMINI_RESPONSE_EMPTY" in code_set:
        return "Gemini가 빈 응답을 반환했습니다. 다시 시도하세요."
    if "GEMINI_RESPONSE_INVALID" in code_set:
        return "Gemini 응답 형식을 검증하지 못했습니다. 다시 시도하세요."
    if "OPENAI_API_KEY_MISSING" in code_set:
        return (
            "OpenAI API 키가 설정되지 않았습니다. "
            "대시보드의 AI 설정에서 키를 저장한 뒤 다시 시도하세요."
        )
    if "OPENAI_API_KEY_OR_REQUEST_INVALID" in code_set:
        return "OpenAI API 키 또는 요청 설정이 올바르지 않습니다. AI 설정을 확인하세요."
    if "OPENAI_QUOTA_EXCEEDED" in code_set:
        return (
            "OpenAI API 사용량 한도를 초과했습니다. "
            "사용량 또는 결제 설정을 확인한 뒤 다시 시도하세요."
        )
    if "OPENAI_PERMISSION_DENIED" in code_set:
        return (
            "OpenAI API 호출 권한이 없습니다. "
            "API 키와 프로젝트 권한을 확인하세요."
        )
    if "OPENAI_MODEL_NOT_FOUND" in code_set:
        return (
            f"선택한 OpenAI 모델 '{model}'을 사용할 수 없습니다. "
            "AI 설정에서 모델을 확인하세요."
        )
    if "OPENAI_NETWORK_ERROR" in code_set:
        return (
            "OpenAI API에 연결하지 못했습니다. "
            "네트워크 연결을 확인한 뒤 다시 시도하세요."
        )
    if "OPENAI_TEMPORARILY_UNAVAILABLE" in code_set:
        return (
            "OpenAI API가 일시적으로 응답하지 않습니다. "
            "잠시 후 다시 시도하세요."
        )
    if "OPENAI_RESPONSE_EMPTY" in code_set:
        return "OpenAI가 빈 응답을 반환했습니다. 다시 시도하세요."
    if "OPENAI_RESPONSE_INVALID" in code_set:
        return "OpenAI 응답 형식을 검증하지 못했습니다. 다시 시도하세요."
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
    codes = [
        str(item.get("code"))
        for item in failure_items
        if isinstance(item, dict) and item.get("code")
    ]
    detail = _safe_provider_error(codes, model)
    if all_failed:
        return detail
    failed_count = len(failure_items)
    if total > 0 and failed_count > 0:
        return f"전체 {total}개 중 {failed_count}개 처리에 실패했습니다. {detail}"
    return f"일부 원본 처리에 실패했습니다. {detail}"


def _safe_translation_error(error: BaseException, model: str) -> str:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    validation = next(
        (
            item
            for item in chain
            if isinstance(item, TranslationValidationError)
        ),
        None,
    )
    if validation is not None:
        compact_cause = " ".join(str(validation).split())
        if len(compact_cause) > 600:
            compact_cause = compact_cause[:597] + "..."
        return (
            "AI 번역 결과가 검증 규칙을 통과하지 못했습니다. "
            f"검증 사유: {compact_cause}"
        )
    code = ai_failure_code(error)
    if code != "SOURCE_PROCESSING_FAILED":
        return _safe_provider_error([code], model)
    return (
        "초벌 번역에 실패했습니다. 완료된 청크는 보존되었습니다. "
        "다시 시도하면 이어서 진행합니다."
    )


def _safe_glossary_error(error: BaseException) -> str:
    if isinstance(error, GlossaryReviewStaleError):
        return (
            "기존 용어 검수 파일이 현재 승인 원문과 일치하지 않습니다. "
            "기존 편집을 별도로 보존하거나 검수 파일을 정리한 뒤 "
            "용어 후보를 다시 생성하세요."
        )
    if isinstance(error, GlossaryBuildError):
        return (
            "승인 원문이 변경되었거나 현재 상태와 맞지 않습니다. "
            "원문 검수를 다시 승인한 뒤 용어 후보를 생성하세요."
        )
    return (
        "용어 후보 생성에 실패했습니다. "
        "승인 원문 상태를 확인한 뒤 다시 시도하세요."
    )


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
    *,
    settings_root: str | Path | None = None,
) -> dict[str, Any]:
    """Acquire a registered source and prepare local review artifacts."""
    source_type = _registered_source_type(project_id, workspace_root)
    progress("등록된 원본을 확인하고 있습니다.", 0, None)
    acquisition: ExtractionResult | ImageOcrRunResult
    if source_type == "pdf":
        pdf_plan = extract_project_pdf(
            project=project_id,
            workspace_root=workspace_root,
            settings_root=settings_root,
            model_name=model,
            dry_run=True,
        )
        selected_pages = list(pdf_plan.selected_pages)
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
            settings_root=settings_root,
            model_name=model,
            progress=report_pdf,
        )
    else:
        image_plan = ocr_project_images(
            project=project_id,
            workspace_root=workspace_root,
            settings_root=settings_root,
            model_name=model,
            dry_run=True,
        )
        total = len(image_plan.selected_images)

        def report_image(message: str) -> None:
            match = _IMAGE_PROGRESS.match(message)
            current = max(0, int(match.group(1)) - 1) if match else None
            message_total = int(match.group(2)) if match else total
            progress(message, current, message_total)

        acquisition = ocr_project_images(
            project=project_id,
            workspace_root=workspace_root,
            settings_root=settings_root,
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
        raise GlossaryReviewStaleError(
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
    *,
    settings_root: str | Path | None = None,
) -> dict[str, Any]:
    """Translate approved source blocks with the current termbase."""
    progress("승인 원문과 용어집을 확인하고 있습니다.", 0, None)
    planned = translate_project(
        project=project_id,
        workspace_root=workspace_root,
        settings_root=settings_root,
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
        settings_root=settings_root,
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
    qa_result = None
    if result.validation_issue_count:
        progress("번역 결과의 확인 필요 항목을 정리하고 있습니다.", total, total)
        qa_result = run_project_translation_qa(
            project=project_id,
            workspace_root=workspace_root,
        )
        progress(
            (
                "초벌 번역이 완료되었습니다. "
                f"번역 검수에서 {qa_result.error_count}개 오류를 확인하세요."
            ),
            total,
            total,
        )
    else:
        progress("초벌 번역과 검수 파일 생성이 완료되었습니다.", total, total)
    return {
        "ok": True,
        "status": "succeeded",
        "translation": result.to_dict(),
        "qa": qa_result.to_dict() if qa_result is not None else None,
        "revision_path": (
            WorkspacePaths(location.path).relative(revision_path)
            if location is not None and revision_path is not None
            else None
        ),
        "review_reset": review_reset,
    }


JobRecordT = TypeVar("JobRecordT", bound=DashboardJobRecord)


class _JobStore(Generic[JobRecordT]):
    """Persist and restore one dashboard job kind."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        state_filename: str,
        state_path: Callable[[WorkspacePaths], Path],
        parse: Callable[[dict[str, Any], str], JobRecordT],
    ) -> None:
        self.workspace_root = workspace_root
        self.state_filename = state_filename
        self._state_path = state_path
        self._parse = parse
        self.records: dict[str, JobRecordT] = {}

    def path_for(self, project_id: str) -> Path:
        location = load_workspace_project_id(
            project_id,
            self.workspace_root,
        )
        return self._state_path(WorkspacePaths(location.path))

    def persist(self, job: JobRecordT) -> None:
        write_json_atomic(self.path_for(job.project_id), job.to_dict())

    def put(self, job: JobRecordT) -> None:
        self.records[job.project_id] = job
        self.persist(job)

    def matching(
        self,
        project_id: str,
        job_id: str,
    ) -> JobRecordT | None:
        job = self.records.get(project_id)
        if job is None or job.job_id != job_id:
            return None
        return job

    def active(self) -> JobRecordT | None:
        return next(
            (
                job
                for job in self.records.values()
                if job.status in ACTIVE_JOB_STATUSES
            ),
            None,
        )

    def list_dicts(self) -> list[dict[str, Any]]:
        jobs = sorted(
            self.records.values(),
            key=lambda job: job.created_at,
            reverse=True,
        )
        return [job.to_dict() for job in jobs]

    def _record_project_id(self, state_path: Path) -> str:
        project_id = state_path.parents[2].name
        return load_workspace_project_id(
            project_id,
            self.workspace_root,
        ).manifest.project_id

    def load(
        self,
        *,
        upgrade: Callable[[JobRecordT], bool] | None = None,
    ) -> None:
        if not self.workspace_root.is_dir():
            return
        pattern = f"*/.glk/state/{self.state_filename}"
        for state_path in self.workspace_root.glob(pattern):
            try:
                value = read_json_object(state_path)
                if value is None:
                    continue
                job = self._parse(
                    value,
                    self._record_project_id(state_path),
                )
                changed = False
                if job.status in ACTIVE_JOB_STATUSES:
                    now = _utc_now()
                    job.status = "interrupted"
                    job.progress_message = (
                        "이전 대시보드가 종료되어 작업 상태를 확인할 수 없습니다."
                    )
                    job.error = (
                        "Dashboard process stopped before job completion."
                    )
                    job.finished_at = now
                    job.updated_at = now
                    changed = True
                if upgrade is not None and upgrade(job):
                    changed = True
                if changed:
                    write_json_atomic(state_path, job.to_dict())
                if job.status in TERMINAL_JOB_STATUSES:
                    self.records[job.project_id] = job
            except (
                OSError,
                UnicodeError,
                TypeError,
                ValueError,
            ):
                continue


class DashboardJobManager:
    """Own one active dashboard job and latest per-project records."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        settings_root: str | Path | None = None,
        runner: SourceJobRunner | None = None,
        glossary_runner: GlossaryJobRunner | None = None,
        translation_runner: TranslationJobRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.settings_root = resolve_settings_root(settings_root)
        self._source_runner = runner
        self._glossary_runner = glossary_runner or run_glossary_pipeline
        self._translation_runner = translation_runner
        self._lock = threading.RLock()
        self._threads: set[threading.Thread] = set()
        self._source_jobs = _JobStore[DashboardSourceJob](
            self.workspace_root,
            state_filename="dashboard_source_job.json",
            state_path=lambda paths: paths.dashboard_source_job_state,
            parse=lambda value, project_id: DashboardSourceJob.from_dict(
                value,
                expected_project_id=project_id,
            ),
        )
        self._glossary_jobs = _JobStore[DashboardGlossaryJob](
            self.workspace_root,
            state_filename="dashboard_glossary_job.json",
            state_path=lambda paths: paths.dashboard_glossary_job_state,
            parse=lambda value, project_id: DashboardGlossaryJob.from_dict(
                value,
                expected_project_id=project_id,
            ),
        )
        self._translation_jobs = _JobStore[DashboardTranslationJob](
            self.workspace_root,
            state_filename="dashboard_translation_job.json",
            state_path=lambda paths: paths.dashboard_translation_job_state,
            parse=lambda value, project_id: DashboardTranslationJob.from_dict(
                value,
                expected_project_id=project_id,
            ),
        )
        self._stores: tuple[_JobStore[Any], ...] = (
            self._source_jobs,
            self._glossary_jobs,
            self._translation_jobs,
        )
        self._closed = False
        self._source_jobs.load(
            upgrade=self._upgrade_acquisition_failure,
        )
        self._glossary_jobs.load()
        self._translation_jobs.load()

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

    def _active_job(
        self,
    ) -> (
        DashboardSourceJob
        | DashboardGlossaryJob
        | DashboardTranslationJob
        | None
    ):
        for store in self._stores:
            active = store.active()
            if active is not None:
                return active
        return None

    def is_project_active(self, project_id: str) -> bool:
        with self._lock:
            return any(
                (job := store.records.get(project_id)) is not None
                and job.status in ACTIVE_JOB_STATUSES
                for store in self._stores
            )

    def _ensure_start_allowed(self, project_id: str) -> None:
        if self._closed:
            raise DashboardJobError("Dashboard job manager is closed.")
        active = self._active_job()
        if active is None:
            return
        if active.project_id == project_id:
            raise DashboardJobConflict(
                "This project already has a background job running."
            )
        raise DashboardJobConflict(
            "Another project background job is already running."
        )

    def _queue_job(
        self,
        store: _JobStore[JobRecordT],
        job: JobRecordT,
        *,
        target: Callable[[str, str], None],
        thread_name: str,
    ) -> dict[str, Any]:
        store.put(job)
        queued_job = job.to_dict()
        def run() -> None:
            try:
                target(job.job_id, job.project_id)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run,
            name=thread_name,
            daemon=True,
        )
        self._threads.add(thread)
        thread.start()
        return queued_job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._source_jobs.list_dicts()

    def list_glossary_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._glossary_jobs.list_dicts()

    def list_translation_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._translation_jobs.list_dicts()

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
            self._ensure_start_allowed(project_id)
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
            return self._queue_job(
                self._source_jobs,
                job,
                target=self._execute_source,
                thread_name=f"glk-source-job-{project_id}",
            )

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
            self._ensure_start_allowed(project_id)
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
            return self._queue_job(
                self._glossary_jobs,
                job,
                target=self._execute_glossary,
                thread_name=f"glk-glossary-job-{project_id}",
            )

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
            self._ensure_start_allowed(project_id)
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
            return self._queue_job(
                self._translation_jobs,
                job,
                target=self._execute_translation,
                thread_name=f"glk-translation-job-{project_id}",
            )

    def _execute_job(
        self,
        store: _JobStore[JobRecordT],
        *,
        job_id: str,
        project_id: str,
        running_message: str,
        run: Callable[[JobRecordT, JobProgress], dict[str, Any]],
        terminal_statuses: frozenset[str],
        invalid_status_message: str,
        result_error: Callable[
            [str, dict[str, Any]],
            str | None,
        ],
        exception_error: Callable[[Exception, JobRecordT], str],
        completion_message: Callable[
            [str, dict[str, Any] | None],
            str,
        ],
        complete_progress_on_success: bool = False,
    ) -> None:
        with self._lock:
            job = store.matching(project_id, job_id)
            if job is None:
                return
            now = _utc_now()
            job.status = "running"
            job.started_at = now
            job.updated_at = now
            job.progress_message = running_message
            store.persist(job)

        def report(
            message: str,
            current: int | None,
            total: int | None,
        ) -> None:
            with self._lock:
                current_job = store.matching(project_id, job_id)
                if current_job is None:
                    return
                current_job.progress_message = message
                if current is not None:
                    current_job.progress_current = current
                if total is not None:
                    current_job.progress_total = total
                current_job.updated_at = _utc_now()
                store.persist(current_job)

        try:
            run_result = run(job, report)
            status = str(run_result.get("status") or "")
            if status not in terminal_statuses:
                raise DashboardJobError(invalid_status_message)
            error = result_error(status, run_result)
            result: dict[str, Any] | None = run_result
        except Exception as caught:
            result = None
            status = "failed"
            error = exception_error(caught, job)

        with self._lock:
            current_job = store.matching(project_id, job_id)
            if current_job is None:
                return
            now = _utc_now()
            current_job.status = status
            current_job.result = result
            current_job.error = error
            current_job.finished_at = now
            current_job.updated_at = now
            current_job.progress_message = completion_message(status, result)
            if complete_progress_on_success and status == "succeeded":
                current_job.progress_current = current_job.progress_total
            store.persist(current_job)

    def _execute_source(self, job_id: str, project_id: str) -> None:
        def run(
            job: DashboardSourceJob,
            report: JobProgress,
        ) -> dict[str, Any]:
            if self._source_runner is not None:
                return self._source_runner(
                    project_id,
                    self.workspace_root,
                    job.model,
                    report,
                )
            return run_registered_source_pipeline(
                project_id,
                self.workspace_root,
                job.model,
                report,
                settings_root=self.settings_root,
            )

        def result_error(
            status: str,
            result: dict[str, Any],
        ) -> str | None:
            error_value = result.get("error")
            if status in {"partial", "failed"} and error_value:
                return str(error_value)
            if status == "partial":
                return "일부 원본 처리에 실패했습니다. 다시 시도하세요."
            if status == "failed":
                return "원문 준비 작업에 실패했습니다. 다시 시도하세요."
            return None

        def completion_message(
            status: str,
            _result: dict[str, Any] | None,
        ) -> str:
            if status == "succeeded":
                return "원문 검수 준비가 완료되었습니다."
            if status == "partial":
                return "일부 원본 처리에 실패했습니다."
            return "원문 준비 작업에 실패했습니다."

        self._execute_job(
            self._source_jobs,
            job_id=job_id,
            project_id=project_id,
            running_message="원문 준비 작업을 시작했습니다.",
            run=run,
            terminal_statuses=frozenset({"succeeded", "partial", "failed"}),
            invalid_status_message=(
                "Source job runner returned an invalid terminal status."
            ),
            result_error=result_error,
            exception_error=lambda caught, job: _safe_provider_error(
                [ai_failure_code(caught)],
                job.model,
            ),
            completion_message=completion_message,
        )

    def _execute_glossary(self, job_id: str, project_id: str) -> None:
        def run(
            _job: DashboardGlossaryJob,
            report: JobProgress,
        ) -> dict[str, Any]:
            return self._glossary_runner(
                project_id,
                self.workspace_root,
                report,
            )

        def result_error(
            status: str,
            result: dict[str, Any],
        ) -> str | None:
            error_value = result.get("error")
            if status == "failed" and error_value:
                return str(error_value)
            if status == "failed":
                return "용어 후보 생성에 실패했습니다. 다시 시도하세요."
            return None

        self._execute_job(
            self._glossary_jobs,
            job_id=job_id,
            project_id=project_id,
            running_message="용어 후보 생성을 시작했습니다.",
            run=run,
            terminal_statuses=frozenset({"succeeded", "failed"}),
            invalid_status_message=(
                "Glossary job runner returned an invalid terminal status."
            ),
            result_error=result_error,
            exception_error=lambda caught, _job: _safe_glossary_error(caught),
            completion_message=lambda status, _result: (
                "용어 후보 생성이 완료되었습니다."
                if status == "succeeded"
                else "용어 후보 생성에 실패했습니다."
            ),
            complete_progress_on_success=True,
        )

    def _execute_translation(
        self,
        job_id: str,
        project_id: str,
    ) -> None:
        def run(
            job: DashboardTranslationJob,
            report: JobProgress,
        ) -> dict[str, Any]:
            if self._translation_runner is not None:
                return self._translation_runner(
                    project_id,
                    self.workspace_root,
                    job.model,
                    job.resume,
                    job.force,
                    report,
                )
            return run_translation_pipeline(
                project_id,
                self.workspace_root,
                job.model,
                job.resume,
                job.force,
                report,
                settings_root=self.settings_root,
            )

        def result_error(
            status: str,
            result: dict[str, Any],
        ) -> str | None:
            error_value = result.get("error")
            if status == "failed" and error_value:
                return str(error_value)
            if status == "failed":
                return "초벌 번역에 실패했습니다. 다시 시도하세요."
            return None

        def completion_message(
            status: str,
            result: dict[str, Any] | None,
        ) -> str:
            if status != "succeeded":
                return "초벌 번역에 실패했습니다."
            qa = result.get("qa") if isinstance(result, dict) else None
            qa_errors = (
                qa.get("error_count")
                if isinstance(qa, dict)
                else None
            )
            if isinstance(qa_errors, int) and qa_errors > 0:
                return (
                    "초벌 번역이 완료되었습니다. "
                    f"번역 검수에서 {qa_errors}개 오류를 확인하세요."
                )
            return "초벌 번역과 검수 파일 생성이 완료되었습니다."

        self._execute_job(
            self._translation_jobs,
            job_id=job_id,
            project_id=project_id,
            running_message="초벌 번역을 시작했습니다.",
            run=run,
            terminal_statuses=frozenset({"succeeded", "failed"}),
            invalid_status_message=(
                "Translation job runner returned an invalid terminal status."
            ),
            result_error=result_error,
            exception_error=lambda caught, job: _safe_translation_error(
                caught,
                job.model,
            ),
            completion_message=completion_message,
            complete_progress_on_success=True,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            threads = tuple(self._threads)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join()
