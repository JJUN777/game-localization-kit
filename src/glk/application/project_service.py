"""Create, load, and inspect project workspaces."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glk.domain.project import ProjectError, ProjectManifest


DEFAULT_WORKSPACE_ROOT = Path("workspaces")
PROJECT_DIRECTORIES = (
    Path("source/pages"),
    Path("segments"),
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
    return {
        "ok": not missing_paths,
        "project_path": str(location.path),
        "manifest": location.manifest.to_dict(),
        "missing_paths": missing_paths,
    }
