"""Register PDF and image originals without running AI acquisition."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Literal
import uuid

from glk.application._hashing import sha256_file
from glk.application._io import copy_file_atomic, write_text_atomic
from glk.application.project_service import (
    ProjectLocation,
    load_project,
    source_processing_started,
    update_project_source,
)
from glk.domain.workspace import (
    IMAGE_SOURCE_ROOT,
    WorkspacePaths,
    is_pdf_source_file,
)


SUPPORTED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_OCR_PROMPT_BYTES = 64 * 1024


class SourceRegistrationError(ValueError):
    """Raised when originals cannot be registered safely."""


class SourceRecoveryError(SourceRegistrationError):
    """Raised when source replacement fails and rollback is incomplete."""

    def __init__(self, backup_path: Path, errors: list[str]) -> None:
        self.backup_path = backup_path
        self.errors = tuple(errors)
        detail = "; ".join(errors)
        super().__init__(
            "원본 교체에 실패했고 기존 원본을 자동 복구하지 못했습니다. "
            f"백업 보존 위치: {backup_path}. 복구 오류: {detail}"
        )


def validate_ocr_prompt(value: str) -> str:
    """Validate a project-wide OCR prompt supplied with image originals."""
    if not value.strip():
        raise SourceRegistrationError("OCR prompt must not be empty.")
    if "\x00" in value:
        raise SourceRegistrationError("OCR prompt must not contain null bytes.")
    if len(value.encode("utf-8")) > MAX_OCR_PROMPT_BYTES:
        raise SourceRegistrationError(
            "OCR prompt must be 64 KiB or smaller."
        )
    return value


def save_project_ocr_prompt(
    location: ProjectLocation,
    value: str,
) -> Path:
    """Update only the image OCR prompt before source processing starts."""
    if location.manifest.source_file != IMAGE_SOURCE_ROOT:
        raise SourceRegistrationError(
            "OCR prompt editing requires a registered image source."
        )
    if source_processing_started(location):
        raise SourceRegistrationError(
            "OCR prompt editing is unavailable after OCR has started."
        )
    prompt_path = WorkspacePaths(location.path).input_ocr_prompt
    write_text_atomic(prompt_path, validate_ocr_prompt(value))
    return prompt_path


def update_project_ocr_prompt(
    *,
    project: str | Path,
    ocr_prompt: str,
    workspace_root: str | Path = "workspaces",
) -> Path:
    """Resolve a project and update only its common image OCR prompt."""
    return save_project_ocr_prompt(
        load_project(project, workspace_root),
        ocr_prompt,
    )


@dataclass(frozen=True, slots=True)
class SourceRegistrationResult:
    project_path: str
    source_type: Literal["pdf", "images"]
    source_file: str
    files: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_path": self.project_path,
            "source_type": self.source_type,
            "source_file": self.source_file,
            "files": list(self.files),
        }


@dataclass(frozen=True, slots=True)
class RegisteredPdfSource:
    location: ProjectLocation
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class RegisteredImageSources:
    location: ProjectLocation
    root: Path
    files: tuple[Path, ...]


def _resolve_file(path: str | Path, *, kind: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise SourceRegistrationError(f"{kind} not found: {candidate}")
    return candidate


def _resolve_folder(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise SourceRegistrationError(f"Image folder not found: {candidate}")
    return candidate


def _natural_key(path: Path, root: Path) -> list[tuple[int, int | str]]:
    relative = path.relative_to(root).as_posix().casefold()
    return [
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", relative)
        if part
    ]


def discover_source_images(folder: Path) -> list[Path]:
    """Return supported non-hidden images in stable natural order."""
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file()
            and not any(
                part.startswith(".")
                for part in path.relative_to(folder).parts
            )
            and path.suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: _natural_key(path, folder),
    )


def project_has_source_files(location: ProjectLocation) -> bool:
    """Return whether either project input folder already contains originals."""
    paths = WorkspacePaths(location.path)
    has_pdf = any(
        path.is_file() and path.suffix.casefold() == ".pdf"
        for path in paths.input_pdf_dir.glob("*")
    )
    return has_pdf or bool(discover_source_images(paths.input_images_dir))


def project_source_replacement_allowed(location: ProjectLocation) -> bool:
    """Return whether registered originals may be replaced before processing."""
    has_source = (
        location.manifest.source_file is not None
        or project_has_source_files(location)
    )
    return has_source and not source_processing_started(location)


def _replace_input_directories(
    location: ProjectLocation,
    register: Callable[
        [],
        RegisteredPdfSource | RegisteredImageSources,
    ],
) -> RegisteredPdfSource | RegisteredImageSources:
    if not project_source_replacement_allowed(location):
        if source_processing_started(location):
            raise SourceRegistrationError(
                "Source replacement is unavailable after extraction or OCR "
                "has started."
            )
        raise SourceRegistrationError(
            "Project has no registered source to replace."
        )

    paths = WorkspacePaths(location.path)
    backup_root = (
        location.path
        / ".glk"
        / f"source-replacement-{uuid.uuid4().hex}"
    )
    backup_pdf = backup_root / "pdf"
    backup_images = backup_root / "images"
    original_source = location.manifest.source_file
    remove_backup = False
    backup_root.mkdir(parents=True)
    try:
        if paths.input_pdf_dir.exists():
            paths.input_pdf_dir.rename(backup_pdf)
        if paths.input_images_dir.exists():
            paths.input_images_dir.rename(backup_images)
        paths.input_pdf_dir.mkdir(parents=True)
        paths.input_images_dir.mkdir(parents=True)

        previous_prompt = backup_images / "ocr_prompt.txt"
        if previous_prompt.is_file():
            copy_file_atomic(previous_prompt, paths.input_ocr_prompt)

        registered = register()
        remove_backup = True
    except Exception as replacement_error:
        recovery_errors: list[str] = []
        for backup, destination in (
            (backup_pdf, paths.input_pdf_dir),
            (backup_images, paths.input_images_dir),
        ):
            if backup.exists():
                try:
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(backup, destination)
                except Exception as recovery_error:
                    recovery_errors.append(
                        f"{destination.name}: {recovery_error}"
                    )
            elif not destination.exists():
                try:
                    destination.mkdir(parents=True)
                except Exception as recovery_error:
                    recovery_errors.append(
                        f"{destination.name}: {recovery_error}"
                    )
        try:
            update_project_source(location, original_source)
        except Exception as recovery_error:
            recovery_errors.append(f"project.json: {recovery_error}")
        if recovery_errors:
            raise SourceRecoveryError(
                backup_root.resolve(),
                recovery_errors,
            ) from replacement_error
        remove_backup = True
        raise
    finally:
        if remove_backup and backup_root.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
    return registered


def replace_pdf_source(
    location: ProjectLocation,
    source_path: str | Path,
) -> RegisteredPdfSource:
    """Replace unprocessed originals with one PDF while preserving config."""
    result = _replace_input_directories(
        location,
        lambda: register_pdf_source(location, source_path, force=True),
    )
    if not isinstance(result, RegisteredPdfSource):
        raise SourceRegistrationError("PDF replacement returned an invalid result.")
    return result


def replace_image_sources(
    location: ProjectLocation,
    source_root: str | Path,
    images: Iterable[str | Path],
    *,
    ocr_prompt: str | None = None,
) -> RegisteredImageSources:
    """Replace unprocessed originals with an image set while preserving config."""
    resolved_images = tuple(images)
    result = _replace_input_directories(
        location,
        lambda: register_image_sources(
            location,
            source_root,
            resolved_images,
            force=True,
            ocr_prompt=ocr_prompt,
        ),
    )
    if not isinstance(result, RegisteredImageSources):
        raise SourceRegistrationError(
            "Image replacement returned an invalid result."
        )
    return result


def validate_image_output_collisions(
    images: Iterable[Path],
    root: Path,
) -> None:
    inputs: dict[str, Path] = {}
    outputs: dict[str, tuple[PurePosixPath, Path]] = {}
    for image_path in images:
        relative = PurePosixPath(image_path.relative_to(root).as_posix())
        input_key = relative.as_posix().casefold()
        previous_input = inputs.get(input_key)
        if previous_input is not None:
            raise SourceRegistrationError(
                "Case-insensitive source filename collision: "
                f"{previous_input.relative_to(root).as_posix()} and "
                f"{relative.as_posix()}"
            )
        inputs[input_key] = image_path

        output = relative.with_suffix(".txt")
        output_key = output.as_posix().casefold()
        previous = outputs.get(output_key)
        if previous is not None:
            previous_output, previous_image = previous
            raise SourceRegistrationError(
                f"Output filename collision: {previous_image.name} and "
                f"{image_path.name} map to case-insensitive output "
                f"{previous_output.as_posix()}"
            )
        outputs[output_key] = (output, image_path)


def register_pdf_source(
    location: ProjectLocation,
    source_path: str | Path,
    *,
    force: bool = False,
) -> RegisteredPdfSource:
    """Copy one PDF into a project and update its manifest."""
    source = _resolve_file(source_path, kind="PDF")
    if source.suffix.casefold() != ".pdf":
        raise SourceRegistrationError(f"Source must be a PDF file: {source}")

    paths = WorkspacePaths(location.path)
    input_dir = paths.input_pdf_dir.resolve()
    destination = source if source.parent == input_dir else input_dir / source.name
    source_file = paths.relative(destination)
    current_source = location.manifest.source_file
    if current_source and current_source != source_file and not force:
        raise SourceRegistrationError(
            f"Project source is already registered as {current_source}. "
            "Use --force to replace it."
        )

    source_hash = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    same_path = source == destination.resolve()
    if destination.exists() and not same_path:
        destination_hash = sha256_file(destination)
        if destination_hash != source_hash and not force:
            raise SourceRegistrationError(
                f"A different {source_file} is already registered. "
                "Use --force to replace the project source."
            )
        if destination_hash != source_hash or force:
            copy_file_atomic(source, destination)
    elif not destination.exists():
        copy_file_atomic(source, destination)

    registered_hash = sha256_file(destination)
    if registered_hash != source_hash:
        raise SourceRegistrationError(
            "Registered PDF hash does not match the input PDF."
        )
    if (
        force
        and current_source != source_file
        and is_pdf_source_file(current_source)
    ):
        previous_source = location.path / str(current_source)
        if previous_source.is_file():
            previous_source.unlink()
    if current_source != source_file:
        location = update_project_source(location, source_file)
    return RegisteredPdfSource(location, destination, registered_hash)


def register_image_sources(
    location: ProjectLocation,
    source_root: str | Path,
    images: Iterable[str | Path],
    *,
    force: bool = False,
    ocr_prompt: str | None = None,
) -> RegisteredImageSources:
    """Copy image originals into a project and update its manifest."""
    validated_prompt = (
        validate_ocr_prompt(ocr_prompt)
        if ocr_prompt is not None
        else None
    )
    root = _resolve_folder(source_root)
    resolved_images = tuple(
        _resolve_file(image, kind="Image")
        for image in images
    )
    if not resolved_images:
        raise SourceRegistrationError(
            f"No supported images found in {root}"
        )

    current_source = location.manifest.source_file
    if current_source not in {None, IMAGE_SOURCE_ROOT} and not force:
        raise SourceRegistrationError(
            f"Project source is already registered as {current_source}. "
            "Use --force to replace the project source type."
        )

    relative_images: list[tuple[Path, Path]] = []
    for image in resolved_images:
        try:
            relative = image.relative_to(root)
        except ValueError as error:
            raise SourceRegistrationError(
                f"Image must be inside the selected folder: {image}"
            ) from error
        if (
            any(part.startswith(".") for part in relative.parts)
            or image.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS
        ):
            raise SourceRegistrationError(
                f"Unsupported source image: {relative.as_posix()}"
            )
        relative_images.append((relative, image))

    relative_images.sort(key=lambda item: _natural_key(item[1], root))
    validate_image_output_collisions(
        [image for _, image in relative_images],
        root,
    )

    destination_root = WorkspacePaths(location.path).input_images_dir
    for relative, source_image in relative_images:
        destination = destination_root / relative
        if source_image.resolve() == destination.resolve():
            continue
        if destination.is_file():
            same = sha256_file(source_image) == sha256_file(destination)
            if not same and not force:
                raise SourceRegistrationError(
                    "A different source image is already registered: "
                    f"{relative.as_posix()}. Use --force to replace it."
                )

    registered: list[Path] = []
    for relative, source_image in relative_images:
        destination = destination_root / relative
        if source_image.resolve() != destination.resolve():
            if not destination.is_file() or (
                sha256_file(source_image) != sha256_file(destination)
            ):
                copy_file_atomic(source_image, destination)
        registered.append(destination)

        sidecar = source_image.with_name(source_image.name + ".prompt.txt")
        if sidecar.is_file():
            destination_sidecar = destination.with_name(
                destination.name + ".prompt.txt"
            )
            if sidecar.resolve() != destination_sidecar.resolve():
                copy_file_atomic(sidecar, destination_sidecar)

    if validated_prompt is not None:
        write_text_atomic(
            WorkspacePaths(location.path).input_ocr_prompt,
            validated_prompt,
        )
    if current_source != IMAGE_SOURCE_ROOT:
        location = update_project_source(location, IMAGE_SOURCE_ROOT)
    return RegisteredImageSources(
        location,
        destination_root,
        tuple(registered),
    )


def register_project_pdf(
    *,
    project: str | Path,
    file: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
) -> SourceRegistrationResult:
    registered = register_pdf_source(
        load_project(project, workspace_root),
        file,
        force=force,
    )
    return SourceRegistrationResult(
        project_path=str(registered.location.path),
        source_type="pdf",
        source_file=str(registered.location.manifest.source_file),
        files=(
            registered.path.relative_to(
                registered.location.path
            ).as_posix(),
        ),
    )


def register_project_images(
    *,
    project: str | Path,
    folder: str | Path,
    workspace_root: str | Path = "workspaces",
    force: bool = False,
    ocr_prompt: str | None = None,
) -> SourceRegistrationResult:
    source_root = _resolve_folder(folder)
    images = discover_source_images(source_root)
    registered = register_image_sources(
        load_project(project, workspace_root),
        source_root,
        images,
        force=force,
        ocr_prompt=ocr_prompt,
    )
    return SourceRegistrationResult(
        project_path=str(registered.location.path),
        source_type="images",
        source_file=IMAGE_SOURCE_ROOT,
        files=tuple(
            path.relative_to(registered.location.path).as_posix()
            for path in registered.files
        ),
    )


def replace_project_pdf(
    *,
    project: str | Path,
    file: str | Path,
    workspace_root: str | Path = "workspaces",
) -> SourceRegistrationResult:
    registered = replace_pdf_source(
        load_project(project, workspace_root),
        file,
    )
    return SourceRegistrationResult(
        project_path=str(registered.location.path),
        source_type="pdf",
        source_file=str(registered.location.manifest.source_file),
        files=(
            registered.path.relative_to(
                registered.location.path
            ).as_posix(),
        ),
    )


def replace_project_images(
    *,
    project: str | Path,
    folder: str | Path,
    workspace_root: str | Path = "workspaces",
    ocr_prompt: str | None = None,
) -> SourceRegistrationResult:
    source_root = _resolve_folder(folder)
    images = discover_source_images(source_root)
    registered = replace_image_sources(
        load_project(project, workspace_root),
        source_root,
        images,
        ocr_prompt=ocr_prompt,
    )
    return SourceRegistrationResult(
        project_path=str(registered.location.path),
        source_type="images",
        source_file=IMAGE_SOURCE_ROOT,
        files=tuple(
            path.relative_to(registered.location.path).as_posix()
            for path in registered.files
        ),
    )
