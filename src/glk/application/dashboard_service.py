"""Build the read-only view model for the local project dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glk.application.project_service import inspect_project, list_projects


DASHBOARD_SCHEMA_VERSION = 1

_STAGE_LABELS = {
    "not_started": "시작 전",
    "source_review": "원문 검수",
    "glossary": "용어 후보 생성",
    "glossary_review": "용어 검수",
    "ready_to_translate": "번역 준비 완료",
    "translation_partial": "번역 진행 중",
    "translation_review": "번역 검수",
    "translation_qa_failed": "번역 QA 확인 필요",
    "completed": "최종 번역 완료",
}

_STAGE_PROGRESS = {
    "not_started": 0,
    "source_review": 25,
    "glossary": 40,
    "glossary_review": 55,
    "ready_to_translate": 70,
    "translation_partial": 78,
    "translation_review": 88,
    "translation_qa_failed": 88,
    "completed": 100,
}


@dataclass(frozen=True, slots=True)
class ReviewAvailability:
    enabled: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "reason": self.reason}


def _review_availability(pipeline: dict[str, Any]) -> dict[str, ReviewAvailability]:
    human_review = pipeline["human_review"]
    source_enabled = human_review in {"pending", "approved"}
    if source_enabled:
        source_reason = "원문 검수 화면을 열 수 있습니다."
    elif human_review == "stale":
        source_reason = "원문 검수 데이터가 오래되었습니다. 원문 준비를 다시 실행하세요."
    else:
        source_reason = "원문 추출과 검수 준비가 먼저 필요합니다."

    glossary_status = pipeline["glossary_status"]
    glossary_enabled = glossary_status == "current"
    if glossary_enabled:
        glossary_reason = "용어 검수 화면을 열 수 있습니다."
    elif glossary_status == "stale":
        glossary_reason = "용어 후보가 오래되었습니다. 용어 후보를 다시 생성하세요."
    else:
        glossary_reason = "원문 승인과 용어 후보 생성이 먼저 필요합니다."

    translation_status = pipeline["translation_status"]
    translation_review = pipeline["translation_review"]
    translation_enabled = (
        translation_status == "current"
        and translation_review not in {"not_ready", "stale"}
    )
    if translation_enabled:
        translation_reason = "번역 검수 화면을 열 수 있습니다."
    elif translation_review == "stale":
        translation_reason = "번역 검수 데이터가 오래되었습니다. 번역 검수를 다시 준비하세요."
    else:
        translation_reason = "용어집 확정과 초벌 번역이 먼저 필요합니다."

    return {
        "source": ReviewAvailability(source_enabled, source_reason),
        "glossary": ReviewAvailability(glossary_enabled, glossary_reason),
        "translation": ReviewAvailability(
            translation_enabled,
            translation_reason,
        ),
    }


def _project_document(summary: Any, status: dict[str, Any]) -> dict[str, Any]:
    pipeline = status["pipeline"]
    reviews = _review_availability(pipeline)
    return {
        "project_id": summary.project_id,
        "name": summary.name,
        "path": summary.path,
        "source_type": summary.source_type,
        "stage": summary.stage,
        "stage_label": _STAGE_LABELS.get(summary.stage, summary.stage),
        "progress": _STAGE_PROGRESS.get(summary.stage, 0),
        "workspace_ready": bool(status["ok"]),
        "missing_paths": list(status["missing_paths"]),
        "pipeline": dict(pipeline),
        "reviews": {
            name: availability.to_dict()
            for name, availability in reviews.items()
        },
    }


def get_dashboard_document(
    workspace_root: str | Path = "workspaces",
) -> dict[str, Any]:
    """Return a read-only snapshot of every valid project workspace."""
    listed = list_projects(workspace_root)
    projects: list[dict[str, Any]] = []
    warnings = [warning.to_dict() for warning in listed.warnings]
    for summary in listed.projects:
        try:
            status = inspect_project(summary.path)
        except (OSError, TypeError, ValueError) as error:
            warnings.append(
                {
                    "directory": summary.project_id,
                    "message": str(error),
                }
            )
            continue
        projects.append(_project_document(summary, status))

    completed = sum(
        bool(project["pipeline"]["final_translation_approved"])
        for project in projects
    )
    needs_attention = sum(
        project["stage"] in {"translation_qa_failed"}
        or not project["workspace_ready"]
        for project in projects
    )
    return {
        "ok": True,
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "workspace_root": listed.workspace_root,
        "summary": {
            "projects": len(projects),
            "in_progress": len(projects) - completed,
            "completed": completed,
            "needs_attention": needs_attention,
        },
        "projects": projects,
        "warnings": warnings,
    }
