"""Create, load, and inspect project workspaces."""

from __future__ import annotations

import json
from importlib.resources import files
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from glk.application._cache import read_json_object
from glk.application._hashing import FileHashCache
from glk.application._hashing import sha256_file_if_exists as _sha256_file
from glk.application._hashing import (
    sha256_text_file_if_exists as _sha256_text_file,
)
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.domain.project import (
    ProjectError,
    ProjectManifest,
    ProjectValidationError,
    normalize_project_id,
)
from glk.domain.workspace import (
    IMAGE_SOURCE_ROOT,
    WORKSPACE_DIRECTORIES,
    WorkspacePaths,
    is_pdf_source_file,
)


DEFAULT_WORKSPACE_ROOT = Path("workspaces")
SOURCE_QA_VERSION = "source-qa-local-v5"
GLOSSARY_BUILD_VERSION = "glossary-candidates-local-v2"
TERMBASE_IMPORT_VERSION = "termbase-import-v1"
TRANSLATION_RUN_VERSION = "translation-run-v1"
TRANSLATION_REVIEW_VERSION = "translation-review-v2"
PROJECT_INPUT_DIRECTORIES = (Path("01_input/pdf"), Path("01_input/images"))
PROJECT_DIRECTORIES = tuple(
    path for path in WORKSPACE_DIRECTORIES if path not in PROJECT_INPUT_DIRECTORIES
)
PROJECT_CREATED_DIRECTORIES = PROJECT_INPUT_DIRECTORIES + PROJECT_DIRECTORIES
DEFAULT_OCR_PROMPT = "01_input/images/ocr_prompt.txt"


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


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    name: str
    source_type: str | None
    stage: str
    final_translation_approved: bool
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectListWarning:
    directory: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectListResult:
    workspace_root: str
    projects: tuple[ProjectSummary, ...]
    warnings: tuple[ProjectListWarning, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "count": len(self.projects),
            "projects": [project.to_dict() for project in self.projects],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    summary: ProjectSummary
    status: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProjectScanResult:
    workspace_root: str
    inspections: tuple[ProjectInspection, ...]
    warnings: tuple[ProjectListWarning, ...]


_FileHash = Callable[[Path], str | None]


@dataclass(frozen=True, slots=True)
class _SourcePipelineStatus:
    source_type: str | None
    source_acquired: bool
    review_source_ready: bool
    qa_status: str
    qa_issues: Any
    human_review: str
    approved_sha256: str | None


@dataclass(frozen=True, slots=True)
class _GlossaryPipelineStatus:
    glossary_status: str
    glossary_candidates: Any
    termbase_status: str
    termbase_entries: Any


@dataclass(frozen=True, slots=True)
class _TranslationRunStatus:
    translation_status: str
    translated_blocks: Any
    state: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _TranslationReviewStatus:
    translation_review: str
    translation_qa_issues: Any


def _resolve_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _default_ocr_prompt_bytes() -> bytes:
    return files("glk.templates").joinpath("ocr_prompt.txt").read_bytes()


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
    created_paths = (
        "project.json",
        *(path.as_posix() for path in PROJECT_CREATED_DIRECTORIES),
        DEFAULT_OCR_PROMPT,
    )
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
        for relative_path in PROJECT_CREATED_DIRECTORIES:
            (staging_path / relative_path).mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_path / "project.json", manifest.to_dict())
        (staging_path / DEFAULT_OCR_PROMPT).write_bytes(
            _default_ocr_prompt_bytes()
        )
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


def load_workspace_project_id(
    project_id: str,
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
) -> ProjectLocation:
    """Load only a direct child project selected by its canonical ID."""
    if not isinstance(project_id, str) or not project_id:
        raise ProjectValidationError("Project ID is required.")
    try:
        normalized_id = normalize_project_id(project_id)
    except ProjectValidationError:
        raise
    if project_id != normalized_id:
        raise ProjectValidationError(
            "Project ID must use lowercase English letters, numbers, and "
            "single underscores only."
        )

    root = _resolve_path(workspace_root)
    candidate = root / project_id
    location = load_project(candidate, root)
    if (
        location.path.parent != root
        or location.path != candidate.resolve()
        or location.manifest.project_id != project_id
    ):
        raise ProjectValidationError(
            "Project must be a direct child of the configured workspace root."
        )
    return location


def update_project_source(
    location: ProjectLocation, source_file: str | None
) -> ProjectLocation:
    manifest = location.manifest.with_source_file(source_file)
    _write_json_atomic(location.path / "project.json", manifest.to_dict())
    return ProjectLocation(location.path, manifest, location.created_paths, location.dry_run)


def source_processing_started(location: ProjectLocation) -> bool:
    """Return whether acquisition or any source-derived work has written files."""
    paths = WorkspacePaths(location.path)
    derived_roots = (
        paths.source_dir,
        location.path / "03_terminology",
        location.path / ".glk/cache",
        paths.segments_dir,
        paths.state_dir,
        location.path / ".glk/reports",
        location.path / "05_output",
    )
    if any(
        path.is_file()
        for root in derived_roots
        if root.is_dir()
        for path in root.rglob("*")
    ):
        return True

    translation_dir = location.path / "04_translation"
    return any(
        path.is_file() and path != paths.translation_prompt
        for path in translation_dir.rglob("*")
    )


def inspect_project(
    project: str | Path,
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    *,
    hash_cache: FileHashCache | None = None,
) -> dict[str, Any]:
    location = load_project(project, workspace_root)
    missing_paths = [
        relative_path.as_posix()
        for relative_path in PROJECT_DIRECTORIES
        if not (location.path / relative_path).is_dir()
    ]
    pipeline = _inspect_pipeline_status(location, hash_cache=hash_cache)
    return {
        "ok": not missing_paths,
        "project_path": str(location.path),
        "manifest": location.manifest.to_dict(),
        "missing_paths": missing_paths,
        "pipeline": pipeline,
    }


def _project_stage(pipeline: dict[str, Any]) -> str:
    if pipeline["final_translation_approved"]:
        return "completed"
    if pipeline["translation_status"] == "partial":
        return "translation_partial"
    if pipeline["translation_review"] == "qa_failed":
        return "translation_qa_failed"
    if pipeline["translation_review"] in {"pending", "stale", "qa_passed"}:
        return "translation_review"
    if pipeline["translation_status"] == "current":
        return "translation_review"
    if pipeline["termbase_status"] == "current":
        return "ready_to_translate"
    if pipeline["glossary_status"] in {"current", "stale"}:
        return "glossary_review"
    if pipeline["final_source_approved"]:
        return "glossary"
    if pipeline["review_source_ready"] or pipeline["source_acquired"]:
        return "source_review"
    return "not_started"


def _project_source_type(project_path: Path, pipeline: dict[str, Any]) -> str | None:
    registered = pipeline["source_type"]
    if registered:
        return str(registered)
    has_pdf = any(
        path.is_file() and path.suffix.casefold() == ".pdf"
        for path in WorkspacePaths(project_path).input_pdf_dir.glob("*")
    )
    image_extensions = {".png", ".jpg", ".jpeg", ".webp"}
    has_images = any(
        path.is_file() and path.suffix.casefold() in image_extensions
        for path in WorkspacePaths(project_path).input_images_dir.rglob("*")
    )
    if has_pdf and has_images:
        return "mixed"
    if has_pdf:
        return "pdf"
    if has_images:
        return "images"
    return None


def scan_projects(
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    *,
    hash_cache: FileHashCache | None = None,
) -> ProjectScanResult:
    """Inspect valid projects once without letting one damaged project abort the scan."""
    root = _resolve_path(workspace_root)
    if not root.exists():
        return ProjectScanResult(str(root), (), ())
    if not root.is_dir():
        raise ProjectError(f"Workspace root is not a directory: {root}")

    inspections: list[ProjectInspection] = []
    warnings: list[ProjectListWarning] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if (
            not candidate.is_dir()
            or candidate.name.startswith(".")
            or not (candidate / "project.json").is_file()
        ):
            continue
        try:
            status = inspect_project(candidate, hash_cache=hash_cache)
            manifest = status["manifest"]
            pipeline = status["pipeline"]
            inspections.append(
                ProjectInspection(
                    summary=ProjectSummary(
                        project_id=manifest["project_id"],
                        name=manifest["name"],
                        source_type=_project_source_type(candidate, pipeline),
                        stage=_project_stage(pipeline),
                        final_translation_approved=bool(
                            pipeline["final_translation_approved"]
                        ),
                        path=str(candidate.resolve()),
                    ),
                    status=status,
                ),
            )
        except (
            ProjectError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            warnings.append(
                ProjectListWarning(directory=candidate.name, message=str(error))
            )
    inspections.sort(
        key=lambda inspection: inspection.summary.project_id.casefold()
    )
    return ProjectScanResult(str(root), tuple(inspections), tuple(warnings))


def list_projects(
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
) -> ProjectListResult:
    """List valid project workspaces without letting one damaged project abort the scan."""
    scanned = scan_projects(workspace_root)
    return ProjectListResult(
        scanned.workspace_root,
        tuple(inspection.summary for inspection in scanned.inspections),
        scanned.warnings,
    )


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    return read_json_object(path)


def _final_translation_files_current(
    project_path: Path,
    state: dict[str, Any],
    legacy_path: Path,
    *,
    file_hash: Callable[[Path], str | None] = _sha256_file,
) -> bool:
    final_files = state.get("final_files")
    if isinstance(final_files, dict) and final_files:
        for relative, expected_hash in final_files.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                return False
            relative_path = PurePosixPath(relative)
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative_path.parts[:1] != ("05_output",)
                or file_hash(project_path / Path(*relative_path.parts))
                != expected_hash
            ):
                return False
        return True
    return state.get("final_sha256") == file_hash(legacy_path)


def _inspect_source_pipeline(
    location: ProjectLocation,
    *,
    paths: WorkspacePaths,
    file_hash: _FileHash,
) -> _SourcePipelineStatus:
    if is_pdf_source_file(location.manifest.source_file):
        acquisition = _read_optional_json(paths.pdf_acquisition_state)
        source_type = "pdf"
    elif location.manifest.source_file == IMAGE_SOURCE_ROOT:
        acquisition = _read_optional_json(paths.image_ocr_state)
        source_type = "images"
    else:
        acquisition = None
        source_type = None
    source_acquired = bool(
        acquisition
        and acquisition.get("status") == "complete"
        and not acquisition.get("failures")
    )

    source_blocks_path = paths.source_segments
    source_sha256 = file_hash(source_blocks_path)
    review_source_ready = source_sha256 is not None
    qa_state = _read_optional_json(paths.source_qa_state)
    if qa_state is None:
        qa_status = "not_run"
        qa_issues = None
    elif (
        qa_state.get("version") != SOURCE_QA_VERSION
        or qa_state.get("source_sha256") != source_sha256
    ):
        qa_status = "stale"
        qa_issues = qa_state.get("total_issues")
    else:
        qa_status = "complete"
        qa_issues = qa_state.get("total_issues")

    review_path = paths.source_review
    review_state = _read_optional_json(paths.source_review_state)
    final_path = paths.source_final
    approved_path = paths.approved_source_segments
    approved_sha256 = file_hash(approved_path)
    if not review_path.is_file() or review_state is None:
        human_review = "not_ready"
    elif review_state.get("source_sha256") != source_sha256:
        human_review = "stale"
    elif (
        review_state.get("status") == "approved"
        and review_state.get("review_sha256") == file_hash(review_path)
        and review_state.get("final_sha256") == file_hash(final_path)
        and review_state.get("approved_blocks_sha256") == approved_sha256
    ):
        human_review = "approved"
    else:
        human_review = "pending"

    return _SourcePipelineStatus(
        source_type=source_type,
        source_acquired=source_acquired,
        review_source_ready=review_source_ready,
        qa_status=qa_status,
        qa_issues=qa_issues,
        human_review=human_review,
        approved_sha256=approved_sha256,
    )


def _inspect_glossary_pipeline(
    *,
    paths: WorkspacePaths,
    source: _SourcePipelineStatus,
    file_hash: _FileHash,
) -> _GlossaryPipelineStatus:
    glossary_path = paths.glossary_review
    glossary_state = _read_optional_json(paths.glossary_build_state)
    if not glossary_path.is_file() or glossary_state is None:
        glossary_status = (
            "not_built"
            if source.human_review == "approved"
            else "not_ready"
        )
        glossary_candidates = None
    elif (
        source.human_review != "approved"
        or glossary_state.get("status") != "complete"
        or glossary_state.get("version") != GLOSSARY_BUILD_VERSION
        or glossary_state.get("approved_source_sha256")
        != source.approved_sha256
    ):
        glossary_status = "stale"
        glossary_candidates = glossary_state.get("candidate_count")
    else:
        glossary_status = "current"
        glossary_candidates = glossary_state.get("candidate_count")

    termbase_path = paths.termbase
    termbase_state = _read_optional_json(paths.glossary_import_state)
    if not termbase_path.is_file() or termbase_state is None:
        termbase_status = "not_built" if glossary_status == "current" else "not_ready"
        termbase_entries = None
    elif (
        glossary_status != "current"
        or termbase_state.get("status") != "complete"
        or termbase_state.get("version") != TERMBASE_IMPORT_VERSION
        or termbase_state.get("approved_source_sha256")
        != source.approved_sha256
        or termbase_state.get("review_tsv_sha256") != file_hash(glossary_path)
        or termbase_state.get("termbase_sha256") != file_hash(termbase_path)
    ):
        termbase_status = "stale"
        termbase_entries = termbase_state.get("entry_count")
    else:
        termbase_status = "current"
        termbase_entries = termbase_state.get("entry_count")

    return _GlossaryPipelineStatus(
        glossary_status=glossary_status,
        glossary_candidates=glossary_candidates,
        termbase_status=termbase_status,
        termbase_entries=termbase_entries,
    )


def _inspect_translation_run(
    *,
    paths: WorkspacePaths,
    source: _SourcePipelineStatus,
    glossary: _GlossaryPipelineStatus,
    file_hash: _FileHash,
    text_file_hash: _FileHash,
) -> _TranslationRunStatus:
    termbase_path = paths.termbase
    translation_path = paths.translation_segments
    translation_state = _read_optional_json(paths.translation_state)
    translation_prompt_path = paths.translation_prompt
    if translation_state is None:
        translation_status = (
            "not_run"
            if glossary.termbase_status == "current"
            else "not_ready"
        )
        translated_blocks = None
    elif (
        glossary.termbase_status != "current"
        or translation_state.get("version") != TRANSLATION_RUN_VERSION
        or translation_state.get("approved_source_sha256")
        != source.approved_sha256
        or translation_state.get("termbase_sha256") != file_hash(termbase_path)
        or translation_state.get("project_prompt_sha256")
        != text_file_hash(translation_prompt_path)
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
        != file_hash(translation_path)
    ):
        translation_status = "stale"
        translated_blocks = translation_state.get("completed_blocks")
    else:
        translation_status = "current"
        translated_blocks = translation_state.get("completed_blocks")

    return _TranslationRunStatus(
        translation_status=translation_status,
        translated_blocks=translated_blocks,
        state=translation_state,
    )


def _inspect_translation_review(
    location: ProjectLocation,
    *,
    paths: WorkspacePaths,
    translation: _TranslationRunStatus,
    file_hash: _FileHash,
) -> _TranslationReviewStatus:
    project_path = location.path
    termbase_path = paths.termbase
    translation_path = paths.translation_segments
    translation_state = translation.state
    translation_review_path = paths.translation_review
    translation_draft_path = paths.translation_draft
    translation_review_state = _read_optional_json(paths.translation_review_state)
    translation_qa_json_path = paths.translation_qa_json
    translation_qa_markdown_path = paths.translation_qa_markdown
    approved_translation_path = paths.approved_translation_segments
    final_translation_path = paths.final_translation_for(
        location.manifest.source_file
    )
    if translation.translation_status != "current":
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
        != file_hash(translation_draft_path)
    ):
        translation_review_status = "stale"
        translation_qa_issues = None
    elif translation_review_state is None:
        translation_review_status = "pending"
        translation_qa_issues = None
    elif (
        translation_review_state.get("version") != TRANSLATION_REVIEW_VERSION
        or translation_review_state.get("translation_output_sha256")
        != file_hash(translation_path)
        or translation_review_state.get("termbase_sha256")
        != file_hash(termbase_path)
        or translation_review_state.get("draft_sha256")
        != file_hash(translation_draft_path)
        or translation_review_state.get("review_sha256")
        != file_hash(translation_review_path)
        or translation_review_state.get("qa_json_sha256")
        != file_hash(translation_qa_json_path)
        or translation_review_state.get("qa_markdown_sha256")
        != file_hash(translation_qa_markdown_path)
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
        and _final_translation_files_current(
            project_path,
            translation_review_state,
            final_translation_path,
            file_hash=file_hash,
        )
    ):
        translation_review_status = "approved"
        translation_qa_issues = translation_review_state.get("error_count")
    else:
        translation_review_status = "stale"
        translation_qa_issues = translation_review_state.get("error_count")

    return _TranslationReviewStatus(
        translation_review=translation_review_status,
        translation_qa_issues=translation_qa_issues,
    )


def _inspect_pipeline_status(
    location: ProjectLocation,
    *,
    hash_cache: FileHashCache | None = None,
) -> dict[str, Any]:
    paths = WorkspacePaths(location.path)
    file_hash = (
        hash_cache.sha256_file_if_exists
        if hash_cache is not None
        else _sha256_file
    )
    text_file_hash = (
        hash_cache.sha256_text_file_if_exists
        if hash_cache is not None
        else _sha256_text_file
    )
    source = _inspect_source_pipeline(
        location,
        paths=paths,
        file_hash=file_hash,
    )
    glossary = _inspect_glossary_pipeline(
        paths=paths,
        source=source,
        file_hash=file_hash,
    )
    translation = _inspect_translation_run(
        paths=paths,
        source=source,
        glossary=glossary,
        file_hash=file_hash,
        text_file_hash=text_file_hash,
    )
    translation_review = _inspect_translation_review(
        location,
        paths=paths,
        translation=translation,
        file_hash=file_hash,
    )
    return {
        "source_type": source.source_type,
        "source_processing_started": source_processing_started(location),
        "source_acquired": source.source_acquired,
        "review_source_ready": source.review_source_ready,
        "qa_status": source.qa_status,
        "qa_issues": source.qa_issues,
        "human_review": source.human_review,
        "final_source_approved": source.human_review == "approved",
        "glossary_status": glossary.glossary_status,
        "glossary_candidates": glossary.glossary_candidates,
        "termbase_status": glossary.termbase_status,
        "termbase_entries": glossary.termbase_entries,
        "translation_status": translation.translation_status,
        "translated_blocks": translation.translated_blocks,
        "translation_review": translation_review.translation_review,
        "translation_qa_issues": translation_review.translation_qa_issues,
        "final_translation_approved": (
            translation_review.translation_review == "approved"
        ),
    }
