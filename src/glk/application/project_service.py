"""Create, load, and inspect project workspaces."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glk.domain.project import ProjectError, ProjectManifest


DEFAULT_WORKSPACE_ROOT = Path("workspaces")
GLOSSARY_BUILD_VERSION = "glossary-candidates-local-v1"
TERMBASE_IMPORT_VERSION = "termbase-import-v1"
TRANSLATION_RUN_VERSION = "translation-run-v1"
TRANSLATION_REVIEW_VERSION = "translation-review-v1"
PROJECT_DIRECTORIES = (
    Path("source/pages"),
    Path("segments"),
    Path("draft"),
    Path("review"),
    Path("final"),
    Path("terminology"),
    Path("qa"),
    Path("revisions"),
    Path("output"),
    Path("state"),
)


class ProjectExistsError(ProjectError):
    """Raised when initialization would overwrite an existing project."""


class ProjectNotFoundError(ProjectError):
    """Raised when a project workspace cannot be found."""


@dataclass(frozen=True, slots=True)
class ProjectLocation:
    path: Path
    manifest: ProjectManifest
    created_paths: tuple[str, ...]
    dry_run: bool = False


def _resolve_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def create_project(
    *,
    name: str,
    project_id: str | None = None,
    profile: str = "default",
    source_language: str = "en",
    target_language: str = "ko",
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    dry_run: bool = False,
) -> ProjectLocation:
    manifest = ProjectManifest.create(
        name=name,
        project_id=project_id,
        profile=profile,
        source_language=source_language,
        target_language=target_language,
    )
    root = _resolve_path(workspace_root)
    project_path = root / manifest.project_id
    created_paths = ("project.json", *(path.as_posix() for path in PROJECT_DIRECTORIES))
    if project_path.exists():
        raise ProjectExistsError(
            f"Project workspace already exists: {project_path}. "
            "Choose another --project-id or use the existing project."
        )
    if dry_run:
        return ProjectLocation(project_path, manifest, created_paths, dry_run=True)

    root.mkdir(parents=True, exist_ok=True)
    staging_path = root / f".{manifest.project_id}.init-{uuid.uuid4().hex}"
    try:
        staging_path.mkdir()
        for relative_path in PROJECT_DIRECTORIES:
            (staging_path / relative_path).mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_path / "project.json", manifest.to_dict())
        staging_path.rename(project_path)
    except Exception:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        raise
    return ProjectLocation(project_path, manifest, created_paths)


def resolve_project_path(
    project: str | Path, workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT
) -> Path:
    reference = Path(project).expanduser()
    explicit_path = (
        reference.is_absolute()
        or reference.parent != Path(".")
        or str(reference) in {".", ".."}
        or reference.name == "project.json"
    )
    if explicit_path:
        candidate = _resolve_path(reference)
    else:
        candidate = _resolve_path(workspace_root) / reference
    if candidate.name == "project.json":
        candidate = candidate.parent
    if not candidate.is_dir() or not (candidate / "project.json").is_file():
        raise ProjectNotFoundError(f"Project workspace not found: {candidate}")
    return candidate


def load_project(
    project: str | Path, workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT
) -> ProjectLocation:
    project_path = resolve_project_path(project, workspace_root)
    manifest_path = project_path / "project.json"
    try:
        with manifest_path.open("r", encoding="utf-8") as file:
            manifest_data = json.load(file)
    except json.JSONDecodeError as error:
        raise ProjectError(f"Invalid JSON in {manifest_path}: {error}") from error
    manifest = ProjectManifest.from_dict(manifest_data)
    return ProjectLocation(project_path, manifest, ())


def update_project_source(
    location: ProjectLocation, source_file: str
) -> ProjectLocation:
    manifest = location.manifest.with_source_file(source_file)
    _write_json_atomic(location.path / "project.json", manifest.to_dict())
    return ProjectLocation(location.path, manifest, location.created_paths, location.dry_run)


def inspect_project(
    project: str | Path, workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT
) -> dict[str, Any]:
    location = load_project(project, workspace_root)
    missing_paths = [
        relative_path.as_posix()
        for relative_path in PROJECT_DIRECTORIES
        if not (location.path / relative_path).is_dir()
    ]
    pipeline = _inspect_pipeline_status(location)
    return {
        "ok": not missing_paths,
        "project_path": str(location.path),
        "manifest": location.manifest.to_dict(),
        "missing_paths": missing_paths,
        "pipeline": pipeline,
    }


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _inspect_pipeline_status(location: ProjectLocation) -> dict[str, Any]:
    project_path = location.path
    if location.manifest.source_file == "source/original.pdf":
        acquisition = _read_optional_json(project_path / "source/document.json")
        source_type = "pdf"
    elif location.manifest.source_file == "source/images":
        acquisition = _read_optional_json(project_path / "source/ocr/run_summary.json")
        source_type = "images"
    else:
        acquisition = None
        source_type = None
    source_acquired = bool(
        acquisition
        and acquisition.get("status") == "complete"
        and not acquisition.get("failures")
    )

    source_blocks_path = project_path / "segments/source.jsonl"
    source_sha256 = _sha256_file(source_blocks_path)
    review_source_ready = source_sha256 is not None

    qa_state = _read_optional_json(project_path / "state/source_qa.json")
    if qa_state is None:
        qa_status = "not_run"
        qa_issues = None
    elif qa_state.get("source_sha256") != source_sha256:
        qa_status = "stale"
        qa_issues = qa_state.get("total_issues")
    else:
        qa_status = "complete"
        qa_issues = qa_state.get("total_issues")

    review_path = project_path / "review/source.txt"
    review_state = _read_optional_json(project_path / "state/source_review.json")
    final_path = project_path / "final/source.txt"
    approved_path = project_path / "segments/approved_source.jsonl"
    approved_sha256 = _sha256_file(approved_path)
    if not review_path.is_file() or review_state is None:
        human_review = "not_ready"
    elif review_state.get("source_sha256") != source_sha256:
        human_review = "stale"
    elif (
        review_state.get("status") == "approved"
        and review_state.get("review_sha256") == _sha256_file(review_path)
        and review_state.get("final_sha256") == _sha256_file(final_path)
        and review_state.get("approved_blocks_sha256") == approved_sha256
    ):
        human_review = "approved"
    else:
        human_review = "pending"

    glossary_path = project_path / "terminology/glossary_review.tsv"
    glossary_state = _read_optional_json(project_path / "state/glossary_build.json")
    if not glossary_path.is_file() or glossary_state is None:
        glossary_status = "not_built" if human_review == "approved" else "not_ready"
        glossary_candidates = None
    elif (
        human_review != "approved"
        or glossary_state.get("status") != "complete"
        or glossary_state.get("version") != GLOSSARY_BUILD_VERSION
        or glossary_state.get("approved_source_sha256") != approved_sha256
    ):
        glossary_status = "stale"
        glossary_candidates = glossary_state.get("candidate_count")
    else:
        glossary_status = "current"
        glossary_candidates = glossary_state.get("candidate_count")

    termbase_path = project_path / "terminology/termbase.json"
    termbase_state = _read_optional_json(project_path / "state/glossary_import.json")
    if not termbase_path.is_file() or termbase_state is None:
        termbase_status = "not_built" if glossary_status == "current" else "not_ready"
        termbase_entries = None
    elif (
        glossary_status != "current"
        or termbase_state.get("status") != "complete"
        or termbase_state.get("version") != TERMBASE_IMPORT_VERSION
        or termbase_state.get("approved_source_sha256") != approved_sha256
        or termbase_state.get("review_tsv_sha256") != _sha256_file(glossary_path)
        or termbase_state.get("termbase_sha256") != _sha256_file(termbase_path)
    ):
        termbase_status = "stale"
        termbase_entries = termbase_state.get("entry_count")
    else:
        termbase_status = "current"
        termbase_entries = termbase_state.get("entry_count")

    translation_path = project_path / "segments/translation.jsonl"
    translation_state = _read_optional_json(project_path / "state/translation.json")
    translation_prompt_path = project_path / "translation_prompt.txt"
    if translation_state is None:
        translation_status = "not_run" if termbase_status == "current" else "not_ready"
        translated_blocks = None
    elif (
        termbase_status != "current"
        or translation_state.get("version") != TRANSLATION_RUN_VERSION
        or translation_state.get("approved_source_sha256") != approved_sha256
        or translation_state.get("termbase_sha256") != _sha256_file(termbase_path)
        or translation_state.get("project_prompt_sha256")
        != _sha256_file(translation_prompt_path)
    ):
        translation_status = "stale"
        translated_blocks = translation_state.get("completed_blocks")
    elif translation_state.get("status") == "partial":
        translation_status = "partial"
        translated_blocks = translation_state.get("completed_blocks")
    elif (
        translation_state.get("status") != "complete"
        or not translation_path.is_file()
        or translation_state.get("translation_output_sha256")
        != _sha256_file(translation_path)
    ):
        translation_status = "stale"
        translated_blocks = translation_state.get("completed_blocks")
    else:
        translation_status = "current"
        translated_blocks = translation_state.get("completed_blocks")

    translation_review_path = project_path / "review/translation.txt"
    translation_draft_path = project_path / "draft/translation.txt"
    translation_review_state = _read_optional_json(
        project_path / "state/translation_review.json"
    )
    translation_qa_json_path = project_path / "qa/translation_qa.json"
    translation_qa_markdown_path = project_path / "qa/translation_qa.md"
    approved_translation_path = (
        project_path / "segments/approved_translation.jsonl"
    )
    final_translation_path = project_path / "final/translation.txt"
    if translation_status != "current":
        translation_review_status = (
            "stale"
            if translation_review_path.is_file()
            or translation_review_state is not None
            else "not_ready"
        )
        translation_qa_issues = (
            translation_review_state.get("error_count")
            if translation_review_state
            else None
        )
    elif (
        translation_state is None
        or translation_state.get("review_status") != "current"
        or translation_state.get("review_base_draft_sha256")
        != _sha256_file(translation_draft_path)
    ):
        translation_review_status = "stale"
        translation_qa_issues = None
    elif translation_review_state is None:
        translation_review_status = "pending"
        translation_qa_issues = None
    elif (
        translation_review_state.get("version") != TRANSLATION_REVIEW_VERSION
        or translation_review_state.get("translation_output_sha256")
        != _sha256_file(translation_path)
        or translation_review_state.get("termbase_sha256")
        != _sha256_file(termbase_path)
        or translation_review_state.get("draft_sha256")
        != _sha256_file(translation_draft_path)
        or translation_review_state.get("review_sha256")
        != _sha256_file(translation_review_path)
        or translation_review_state.get("qa_json_sha256")
        != _sha256_file(translation_qa_json_path)
        or translation_review_state.get("qa_markdown_sha256")
        != _sha256_file(translation_qa_markdown_path)
    ):
        translation_review_status = "stale"
        translation_qa_issues = translation_review_state.get("error_count")
    elif translation_review_state.get("status") == "qa_failed":
        translation_review_status = "qa_failed"
        translation_qa_issues = translation_review_state.get("error_count")
    elif translation_review_state.get("status") == "qa_passed":
        translation_review_status = "qa_passed"
        translation_qa_issues = translation_review_state.get("error_count")
    elif (
        translation_review_state.get("status") == "approved"
        and translation_review_state.get("approved_segments_sha256")
        == _sha256_file(approved_translation_path)
        and translation_review_state.get("final_sha256")
        == _sha256_file(final_translation_path)
    ):
        translation_review_status = "approved"
        translation_qa_issues = translation_review_state.get("error_count")
    else:
        translation_review_status = "stale"
        translation_qa_issues = translation_review_state.get("error_count")

    return {
        "source_type": source_type,
        "source_acquired": source_acquired,
        "review_source_ready": review_source_ready,
        "qa_status": qa_status,
        "qa_issues": qa_issues,
        "human_review": human_review,
        "final_source_approved": human_review == "approved",
        "glossary_status": glossary_status,
        "glossary_candidates": glossary_candidates,
        "termbase_status": termbase_status,
        "termbase_entries": termbase_entries,
        "translation_status": translation_status,
        "translated_blocks": translated_blocks,
        "translation_review": translation_review_status,
        "translation_qa_issues": translation_qa_issues,
        "final_translation_approved": translation_review_status == "approved",
    }
