"""Archive and reset translation-review artifacts for deliberate full restarts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from glk.application._hashing import sha256_file_if_exists
from glk.application._io import copy_file_atomic, write_json_atomic
from glk.application.project_service import ProjectLocation
from glk.domain.workspace import WorkspacePaths


def _restart_revision_path(project_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        WorkspacePaths(project_path).translation_revisions
        / f"translation_restart_{stamp}"
    )


def _restart_files(paths: WorkspacePaths) -> list[Path]:
    files = [
        paths.translation_prompt,
        paths.translation_segments,
        paths.translation_state,
        paths.translation_draft,
        paths.translation_review,
        paths.translation_review_state,
        paths.translation_qa_json,
        paths.translation_qa_markdown,
        paths.approved_translation_segments,
    ]
    output_root = paths.root / "05_output"
    if output_root.is_dir():
        files.extend(path for path in output_root.rglob("*") if path.is_file())
    return sorted(
        {path.resolve() for path in files if path.is_file()},
        key=lambda path: path.relative_to(paths.root.resolve()).as_posix(),
    )


def archive_translation_restart(
    location: ProjectLocation,
) -> Path | None:
    """Copy the current translation workflow into a timestamped revision."""
    paths = WorkspacePaths(location.path)
    files = _restart_files(paths)
    if not files:
        return None
    revision_root = _restart_revision_path(location.path)
    records: list[dict[str, str]] = []
    for source in files:
        relative = source.relative_to(location.path.resolve())
        destination = revision_root / relative
        copy_file_atomic(source, destination)
        records.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file_if_exists(source) or "",
            }
        )
    write_json_atomic(
        revision_root / "manifest.json",
        {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "project_id": location.manifest.project_id,
            "reason": "full_translation_restart",
            "files": records,
        },
    )
    return revision_root


def clear_stale_translation_review_artifacts(
    location: ProjectLocation,
) -> None:
    """Remove obsolete approval state after a successful full retranslation."""
    paths = WorkspacePaths(location.path)
    for path in (
        paths.translation_review_state,
        paths.translation_qa_json,
        paths.translation_qa_markdown,
        paths.approved_translation_segments,
    ):
        path.unlink(missing_ok=True)
    output_root = location.path / "05_output"
    if not output_root.is_dir():
        return
    for path in sorted(
        output_root.rglob("*"),
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
