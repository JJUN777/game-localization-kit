"""Build the read-only view model for the local project dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from glk.application._hashing import FileHashCache, sha256_file_if_exists
from glk.application.ai_usage_ledger import summarize_project_ai_usage
from glk.application.project_service import (
    inspect_project,
    load_workspace_project_id,
    scan_projects,
)
from glk.application.source_registration_service import (
    discover_source_images,
    project_source_recovery_replacement_allowed,
)
from glk.application.translation_prompt_service import (
    TranslationPromptError,
    load_translation_prompt_document,
)
from glk.domain.source_block import SourceBlock, SourceBlockValidationError
from glk.domain.workspace import WorkspacePaths, is_pdf_source_file


DASHBOARD_SCHEMA_VERSION = 1
_SOURCE_SECTION_SEPARATOR = "======================"
_ACTIVE_PREVIOUS_OUTPUT_ID = "active"
_TRANSLATION_RESTART_REVISION_PATTERN = re.compile(
    r"translation_restart_\d{8}T\d{6}\.\d{6}Z"
)

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


class DashboardOutputError(ValueError):
    """Raised when a dashboard output cannot be downloaded safely."""


@dataclass(frozen=True, slots=True)
class DashboardOutput:
    path: Path
    relative_path: str
    name: str
    download_name: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "name": self.name,
            "download_name": self.download_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DashboardPreviousOutputVersion:
    revision_id: str
    approved_at: str | None
    archived_at: str | None
    outputs: tuple[DashboardOutput, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "approved_at": self.approved_at,
            "archived_at": self.archived_at,
            "outputs": [output.to_dict() for output in self.outputs],
        }


@dataclass(frozen=True, slots=True)
class DashboardSourceOutput:
    data: bytes
    relative_path: str
    name: str
    download_name: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "name": self.name,
            "download_name": self.download_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DashboardOutputArchive:
    data: bytes
    download_name: str
    file_count: int


def _approved_outputs(
    project_path: Path,
    *,
    hash_cache: FileHashCache | None = None,
) -> tuple[DashboardOutput, ...]:
    paths = WorkspacePaths(project_path)
    try:
        state = json.loads(
            paths.translation_review_state.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DashboardOutputError(
            "최종 번역 승인 상태를 읽을 수 없습니다."
        ) from error
    final_files = state.get("final_files") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or state.get("status") != "approved"
        or not isinstance(final_files, dict)
    ):
        raise DashboardOutputError("최종 승인된 번역 결과가 없습니다.")

    output_root = (project_path / "05_output").resolve()
    outputs: list[DashboardOutput] = []
    for relative, expected_hash in final_files.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise DashboardOutputError("최종 번역 파일 정보가 올바르지 않습니다.")
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.parts[:1] != ("05_output",)
            or len(relative_path.parts) < 2
        ):
            raise DashboardOutputError("최종 번역 파일 경로가 올바르지 않습니다.")
        candidate = (project_path / Path(*relative_path.parts)).resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError as error:
            raise DashboardOutputError(
                "최종 번역 파일이 출력 폴더 밖에 있습니다."
            ) from error
        if (
            not candidate.is_file()
            or (
                hash_cache.sha256_file_if_exists(candidate)
                if hash_cache is not None
                else sha256_file_if_exists(candidate)
            )
            != expected_hash
        ):
            raise DashboardOutputError(
                "최종 번역 파일이 승인 이후 변경되었습니다."
            )
        name = PurePosixPath(*relative_path.parts[1:]).as_posix()
        outputs.append(
            DashboardOutput(
                path=candidate,
                relative_path=relative_path.as_posix(),
                name=name,
                download_name=relative_path.name,
                size_bytes=candidate.stat().st_size,
                sha256=expected_hash,
            )
        )
    if not outputs:
        raise DashboardOutputError("최종 승인된 번역 결과가 없습니다.")
    return tuple(
        sorted(
            outputs,
            key=lambda output: (
                output.name != "combined_kor.txt",
                output.name.casefold(),
            ),
        )
    )


def _approved_output_time(project_path: Path) -> str | None:
    try:
        state = json.loads(
            WorkspacePaths(project_path).translation_review_state.read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    approved_at = state.get("approved_at") if isinstance(state, dict) else None
    return approved_at if isinstance(approved_at, str) else None


def _timestamped_previous_outputs(
    outputs: tuple[DashboardOutput, ...],
    timestamp: str | None,
) -> tuple[DashboardOutput, ...]:
    try:
        parsed = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        stamp = parsed.astimezone().strftime("%Y%m%d_%H%M%S")
    except ValueError:
        stamp = "previous"
    timestamped: list[DashboardOutput] = []
    for output in outputs:
        name = PurePosixPath(output.download_name)
        timestamped.append(
            DashboardOutput(
                path=output.path,
                relative_path=output.relative_path,
                name=output.name,
                download_name=f"{name.stem}_{stamp}{name.suffix}",
                size_bytes=output.size_bytes,
                sha256=output.sha256,
            )
        )
    return tuple(timestamped)


def _translation_restart_revision(
    project_path: Path,
    project_id: str,
    revision_id: str,
) -> tuple[Path, str | None]:
    if not _TRANSLATION_RESTART_REVISION_PATTERN.fullmatch(revision_id):
        raise DashboardOutputError("이전 번역본 이력이 올바르지 않습니다.")
    revisions_root = WorkspacePaths(project_path).translation_revisions.resolve()
    unresolved_revision = revisions_root / revision_id
    if unresolved_revision.is_symlink():
        raise DashboardOutputError("이전 번역본 이력이 올바르지 않습니다.")
    revision_root = unresolved_revision.resolve()
    try:
        revision_root.relative_to(revisions_root)
    except ValueError as error:
        raise DashboardOutputError(
            "이전 번역본 이력이 프로젝트 폴더 밖에 있습니다."
        ) from error
    if revision_root.parent != revisions_root or not revision_root.is_dir():
        raise DashboardOutputError("이전 번역본 이력을 찾지 못했습니다.")
    try:
        manifest = json.loads(
            (revision_root / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DashboardOutputError(
            "이전 번역본 이력을 확인할 수 없습니다."
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("project_id") != project_id
        or manifest.get("reason") != "full_translation_restart"
    ):
        raise DashboardOutputError("이전 번역본 이력이 올바르지 않습니다.")
    archived_at = manifest.get("created_at")
    return (
        revision_root,
        archived_at if isinstance(archived_at, str) else None,
    )


def _previous_output_versions(
    project_path: Path,
    project_id: str,
    *,
    current_is_approved: bool,
    hash_cache: FileHashCache | None = None,
) -> tuple[DashboardPreviousOutputVersion, ...]:
    versions: list[DashboardPreviousOutputVersion] = []
    active_signature: tuple[tuple[str, str], ...] | None = None
    if not current_is_approved:
        try:
            outputs = _approved_outputs(project_path, hash_cache=hash_cache)
        except DashboardOutputError:
            pass
        else:
            approved_at = _approved_output_time(project_path)
            active_signature = tuple(
                (output.relative_path, output.sha256) for output in outputs
            )
            versions.append(
                DashboardPreviousOutputVersion(
                    revision_id=_ACTIVE_PREVIOUS_OUTPUT_ID,
                    approved_at=approved_at,
                    archived_at=None,
                    outputs=_timestamped_previous_outputs(
                        outputs,
                        approved_at,
                    ),
                )
            )

    revisions_root = WorkspacePaths(project_path).translation_revisions
    if not revisions_root.is_dir():
        return tuple(versions)
    revision_ids = sorted(
        (
            path.name
            for path in revisions_root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and _TRANSLATION_RESTART_REVISION_PATTERN.fullmatch(path.name)
        ),
        reverse=True,
    )
    for revision_id in revision_ids:
        try:
            revision_root, archived_at = _translation_restart_revision(
                project_path,
                project_id,
                revision_id,
            )
            outputs = _approved_outputs(
                revision_root,
                hash_cache=hash_cache,
            )
        except DashboardOutputError:
            continue
        signature = tuple(
            (output.relative_path, output.sha256) for output in outputs
        )
        if active_signature is not None and signature == active_signature:
            continue
        approved_at = _approved_output_time(revision_root)
        versions.append(
            DashboardPreviousOutputVersion(
                revision_id=revision_id,
                approved_at=approved_at,
                archived_at=archived_at,
                outputs=_timestamped_previous_outputs(
                    outputs,
                    approved_at or archived_at,
                ),
            )
        )
    return tuple(versions)


def _parse_approved_source_blocks(data: bytes) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    line_number = 0
    try:
        for line_number, line in enumerate(
            data.decode("utf-8").splitlines(),
            start=1,
        ):
            if line.strip():
                blocks.append(SourceBlock.from_dict(json.loads(line)))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceBlockValidationError,
        TypeError,
    ) as error:
        raise DashboardOutputError(
            f"승인된 원문 블록 {line_number}번 줄이 올바르지 않습니다."
        ) from error
    if not blocks or len({block.id for block in blocks}) != len(blocks):
        raise DashboardOutputError("승인된 원문 블록이 올바르지 않습니다.")
    return blocks


def _render_approved_source_text(blocks: list[SourceBlock]) -> bytes:
    grouped: dict[tuple[str, str | int], list[str]] = {}
    for block in sorted(blocks, key=lambda item: item.source_order):
        text = block.effective_text.strip()
        if not text:
            raise DashboardOutputError("승인된 원문 블록에 빈 본문이 있습니다.")
        block_locator: tuple[str, str | int] = (
            ("page", block.page)
            if block.page is not None
            else ("source", block.source_file)
        )
        grouped.setdefault(block_locator, []).append(text)

    lines: list[str] = []
    for index, (section_locator, texts) in enumerate(grouped.items()):
        if index:
            lines.extend((_SOURCE_SECTION_SEPARATOR, ""))
        lines.append(
            f"[PAGE {section_locator[1]}]"
            if section_locator[0] == "page"
            else f"[{PurePosixPath(str(section_locator[1])).name}]"
        )
        lines.append("")
        for text in texts:
            lines.extend((text, ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _approved_source_output(
    project_path: Path,
    project_id: str,
) -> DashboardSourceOutput:
    paths = WorkspacePaths(project_path)
    try:
        state = json.loads(
            paths.source_review_state.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DashboardOutputError(
            "최종 원문 승인 상태를 읽을 수 없습니다."
        ) from error
    expected_hash = (
        state.get("approved_blocks_sha256")
        if isinstance(state, dict)
        else None
    )
    if (
        not isinstance(state, dict)
        or state.get("status") != "approved"
        or not isinstance(expected_hash, str)
    ):
        raise DashboardOutputError("최종 승인된 원문이 없습니다.")

    segments_root = paths.segments_dir.resolve()
    candidate = paths.approved_source_segments.resolve()
    try:
        candidate.relative_to(segments_root)
    except ValueError as error:
        raise DashboardOutputError(
            "승인된 원문 블록이 프로젝트 폴더 밖에 있습니다."
        ) from error
    try:
        approved_data = candidate.read_bytes()
    except OSError as error:
        raise DashboardOutputError(
            "승인된 원문 블록을 읽을 수 없습니다."
        ) from error
    if hashlib.sha256(approved_data).hexdigest() != expected_hash:
        raise DashboardOutputError(
            "승인된 원문 블록이 승인 이후 변경되었습니다."
        )
    data = _render_approved_source_text(
        _parse_approved_source_blocks(approved_data)
    )
    return DashboardSourceOutput(
        data=data,
        relative_path=paths.relative(paths.source_final),
        name=paths.source_final.name,
        download_name=f"{project_id}_source.txt",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def get_project_dashboard_source_output(
    *,
    project_id: str,
    workspace_root: str | Path = "workspaces",
) -> DashboardSourceOutput:
    """Resolve the current approved source text for dashboard download."""
    location = load_workspace_project_id(project_id, workspace_root)
    hash_cache = FileHashCache()
    status = inspect_project(location.path, hash_cache=hash_cache)
    if not status["pipeline"]["final_source_approved"]:
        raise DashboardOutputError("현재 승인된 최종 원문이 없습니다.")
    return _approved_source_output(
        location.path,
        project_id,
    )


def get_project_dashboard_output(
    *,
    project_id: str,
    output_path: str,
    workspace_root: str | Path = "workspaces",
) -> DashboardOutput:
    """Resolve one current approved output selected from the dashboard."""
    if not isinstance(output_path, str) or not output_path:
        raise DashboardOutputError("다운로드할 결과 파일을 선택하세요.")
    location = load_workspace_project_id(project_id, workspace_root)
    hash_cache = FileHashCache()
    status = inspect_project(location.path, hash_cache=hash_cache)
    if not status["pipeline"]["final_translation_approved"]:
        raise DashboardOutputError("현재 승인된 최종 번역 결과가 없습니다.")
    for output in _approved_outputs(location.path, hash_cache=hash_cache):
        if output.relative_path == output_path:
            return output
    raise DashboardOutputError("다운로드할 결과 파일을 찾지 못했습니다.")


def get_project_dashboard_previous_output(
    *,
    project_id: str,
    revision_id: str,
    output_path: str,
    workspace_root: str | Path = "workspaces",
) -> DashboardOutput:
    """Resolve one previous approved output selected from the dashboard."""
    if not isinstance(revision_id, str) or not revision_id:
        raise DashboardOutputError("이전 번역본 이력을 선택하세요.")
    if not isinstance(output_path, str) or not output_path:
        raise DashboardOutputError("다운로드할 이전 번역 파일을 선택하세요.")
    location = load_workspace_project_id(project_id, workspace_root)
    hash_cache = FileHashCache()
    status = inspect_project(location.path, hash_cache=hash_cache)
    versions = _previous_output_versions(
        location.path,
        project_id,
        current_is_approved=bool(
            status["pipeline"]["final_translation_approved"]
        ),
        hash_cache=hash_cache,
    )
    for version in versions:
        if version.revision_id != revision_id:
            continue
        for output in version.outputs:
            if output.relative_path == output_path:
                return output
        break
    raise DashboardOutputError("다운로드할 이전 번역 파일을 찾지 못했습니다.")


def get_project_dashboard_image_output_archive(
    *,
    project_id: str,
    workspace_root: str | Path = "workspaces",
) -> DashboardOutputArchive:
    """Build a ZIP containing every approved per-image translation output."""
    location = load_workspace_project_id(project_id, workspace_root)
    hash_cache = FileHashCache()
    status = inspect_project(location.path, hash_cache=hash_cache)
    if not status["pipeline"]["final_translation_approved"]:
        raise DashboardOutputError("현재 승인된 최종 번역 결과가 없습니다.")

    outputs = _approved_outputs(location.path, hash_cache=hash_cache)
    if not any(output.name == "combined_kor.txt" for output in outputs):
        raise DashboardOutputError(
            "이미지 프로젝트에서만 이미지별 결과를 받을 수 있습니다."
        )
    image_outputs = tuple(
        output for output in outputs if output.name != "combined_kor.txt"
    )
    if not image_outputs:
        raise DashboardOutputError("이미지별 번역 결과가 없습니다.")

    archive_buffer = BytesIO()
    with ZipFile(archive_buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        for output in image_outputs:
            try:
                data = output.path.read_bytes()
            except OSError as error:
                raise DashboardOutputError(
                    "이미지별 번역 파일을 읽을 수 없습니다."
                ) from error
            if hashlib.sha256(data).hexdigest() != output.sha256:
                raise DashboardOutputError(
                    "최종 번역 파일이 승인 이후 변경되었습니다."
                )
            archive.writestr(output.name, data)

    return DashboardOutputArchive(
        data=archive_buffer.getvalue(),
        download_name=f"{project_id}_image_outputs.zip",
        file_count=len(image_outputs),
    )


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
    if translation_review == "qa_failed":
        issue_count = pipeline.get("translation_qa_issues")
        translation_reason = (
            f"번역 QA 오류 {issue_count}개를 검수 화면에서 수정하세요."
            if isinstance(issue_count, int)
            else "번역 QA 오류를 검수 화면에서 수정하세요."
        )
    elif translation_enabled:
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


def _project_source_files(summary: Any, status: dict[str, Any]) -> list[str]:
    paths = WorkspacePaths(Path(summary.path))
    source_type = summary.source_type
    files: list[str] = []
    if source_type in {"pdf", "mixed"}:
        source_file = status["manifest"].get("source_file")
        if (
            isinstance(source_file, str)
            and is_pdf_source_file(source_file)
            and (paths.root / Path(source_file)).is_file()
        ):
            files.append(Path(source_file).name)
        else:
            files.extend(
                path.name
                for path in sorted(
                    paths.input_pdf_dir.glob("*.pdf"),
                    key=lambda value: value.name.casefold(),
                )
                if path.is_file()
            )
    if source_type in {"images", "mixed"}:
        files.extend(
            path.relative_to(paths.input_images_dir).as_posix()
            for path in discover_source_images(paths.input_images_dir)
        )
    return files


def _project_ocr_prompt(summary: Any) -> str:
    prompt_path = WorkspacePaths(Path(summary.path)).input_ocr_prompt
    if not prompt_path.is_file():
        return ""
    return prompt_path.read_text(encoding="utf-8")


def _project_translation_prompt(summary: Any) -> dict[str, Any]:
    try:
        return load_translation_prompt_document(Path(summary.path)).to_dict()
    except TranslationPromptError:
        return {"value": "", "saved": True, "sha256": ""}


def _project_document(
    summary: Any,
    status: dict[str, Any],
    *,
    hash_cache: FileHashCache,
) -> dict[str, Any]:
    pipeline = status["pipeline"]
    reviews = _review_availability(pipeline)
    outputs: tuple[DashboardOutput, ...] = ()
    if pipeline["final_translation_approved"]:
        try:
            outputs = _approved_outputs(
                Path(summary.path),
                hash_cache=hash_cache,
            )
        except DashboardOutputError:
            outputs = ()
    previous_outputs = _previous_output_versions(
        Path(summary.path),
        summary.project_id,
        current_is_approved=bool(pipeline["final_translation_approved"]),
        hash_cache=hash_cache,
    )
    source_output: DashboardSourceOutput | None = None
    if pipeline["final_source_approved"]:
        try:
            source_output = _approved_source_output(
                Path(summary.path),
                summary.project_id,
            )
        except DashboardOutputError:
            source_output = None
    recovery_replacement_allowed = bool(
        summary.source_type
        and project_source_recovery_replacement_allowed(
            load_workspace_project_id(summary.project_id, Path(summary.path).parent)
        )
    )
    replacement_allowed = bool(
        summary.source_type
        and (
            not pipeline["source_processing_started"]
            or recovery_replacement_allowed
        )
    )
    if recovery_replacement_allowed:
        replacement_reason = (
            "처리에 실패한 원본을 교체할 수 있습니다. 아직 검수 내용은 없습니다."
        )
    elif replacement_allowed:
        replacement_reason = "원문 추출·OCR 시작 전까지 원본을 교체할 수 있습니다."
    elif summary.source_type:
        replacement_reason = "원문 추출·OCR이 시작되어 원본을 교체할 수 없습니다."
    else:
        replacement_reason = "먼저 PDF 또는 이미지 원본을 등록하세요."
    prompt_edit_allowed = bool(
        summary.source_type == "images"
        and not pipeline["source_processing_started"]
    )
    if prompt_edit_allowed:
        prompt_edit_reason = "OCR 시작 전까지 공통 프롬프트를 수정할 수 있습니다."
    elif summary.source_type == "images":
        prompt_edit_reason = "OCR이 시작되어 공통 프롬프트를 수정할 수 없습니다."
    else:
        prompt_edit_reason = "이미지 원본 프로젝트에서만 사용할 수 있습니다."
    return {
        "project_id": summary.project_id,
        "name": summary.name,
        "path": summary.path,
        "source_type": summary.source_type,
        "source_files": _project_source_files(summary, status),
        "ocr_prompt": _project_ocr_prompt(summary),
        "translation_prompt": _project_translation_prompt(summary),
        "source_output": (
            source_output.to_dict() if source_output is not None else None
        ),
        "outputs": [output.to_dict() for output in outputs],
        "previous_outputs": [
            version.to_dict() for version in previous_outputs
        ],
        "ai_usage": summarize_project_ai_usage(Path(summary.path)),
        "stage": summary.stage,
        "stage_label": _STAGE_LABELS.get(summary.stage, summary.stage),
        "progress": _STAGE_PROGRESS.get(summary.stage, 0),
        "workspace_ready": bool(status["ok"]),
        "missing_paths": list(status["missing_paths"]),
        "pipeline": dict(pipeline),
        "source_replacement": {
            "allowed": replacement_allowed,
            "reason": replacement_reason,
        },
        "ocr_prompt_edit": {
            "allowed": prompt_edit_allowed,
            "reason": prompt_edit_reason,
        },
        "reviews": {
            name: availability.to_dict()
            for name, availability in reviews.items()
        },
    }


def get_dashboard_document(
    workspace_root: str | Path = "workspaces",
) -> dict[str, Any]:
    """Return a read-only snapshot of every valid project workspace."""
    hash_cache = FileHashCache()
    scanned = scan_projects(workspace_root, hash_cache=hash_cache)
    projects: list[dict[str, Any]] = []
    warnings = [warning.to_dict() for warning in scanned.warnings]
    for inspection in scanned.inspections:
        projects.append(
            _project_document(
                inspection.summary,
                inspection.status,
                hash_cache=hash_cache,
            )
        )

    completed = sum(
        bool(project["pipeline"]["final_translation_approved"])
        for project in projects
    )
    in_progress = sum(
        project["stage"] not in {"not_started", "completed"}
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
        "workspace_root": scanned.workspace_root,
        "summary": {
            "projects": len(projects),
            "in_progress": in_progress,
            "completed": completed,
            "needs_attention": needs_attention,
        },
        "projects": projects,
        "warnings": warnings,
    }
