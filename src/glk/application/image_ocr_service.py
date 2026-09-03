"""Project-level image registration and structured OCR orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

from glk.application._cache import invalid_cache, read_json_object
from glk.application._hashing import sha256_file as _sha256_file
from glk.application._hashing import sha256_text as _sha256_text
from glk.application._io import copy_file_atomic as _copy_file_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application._io import write_text_atomic as _write_text_atomic
from glk.application._progress import (
    ProgressCallback,
    ProgressCallbackError,
    guard_progress_callback,
)
from glk.application.project_service import (
    ProjectLocation,
    load_project,
)
from glk.application.source_registration_service import (
    SUPPORTED_IMAGE_EXTENSIONS,
    SourceRegistrationError,
    discover_source_images,
    register_image_sources,
    validate_image_output_collisions,
)
from glk.domain.workspace import IMAGE_SOURCE_ROOT, WorkspacePaths
from glk.extraction.image_ocr import (
    build_combined_text,
    build_individual_text,
    build_ocr_prompt,
    validate_ocr_result,
)
from glk.infrastructure.ai_provider import (
    ai_failure_code,
    create_image_ocr_provider,
)
from glk.infrastructure.ai_usage import provider_usage


IMAGE_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS


class ImageOcrError(ValueError):
    """Raised when image input cannot be registered or processed safely."""


class ImageOcrProvider(Protocol):
    model_name: str
    prompt_version: str

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ImageOcrFailure:
    file: str
    error: str
    code: str = "SOURCE_PROCESSING_FAILED"


@dataclass(frozen=True, slots=True)
class _RegisteredOcrInput:
    location: ProjectLocation
    folder: Path
    images: tuple[Path, ...]
    prompt: Path | None


@dataclass(frozen=True, slots=True)
class _ImageOcrOutput:
    source_name: str
    text_name: str
    text: str
    cached: bool
    needs_review: bool


@dataclass(frozen=True, slots=True)
class _ImageOcrBatch:
    successful: tuple[_ImageOcrOutput, ...]
    combined_items: tuple[tuple[str, str], ...]
    failures: tuple[ImageOcrFailure, ...]


@dataclass(frozen=True, slots=True)
class ImageOcrRunResult:
    project_path: str
    source_folder: str
    prompt_file: str | None
    model: str | None
    prompt_version: str | None
    selected_images: tuple[str, ...]
    successful_images: tuple[str, ...]
    cached_images: tuple[str, ...]
    needs_review: tuple[str, ...]
    failures: tuple[ImageOcrFailure, ...]
    output_file: str | None
    dry_run: bool = False
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = self.ok
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_folder(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise ImageOcrError(f"Image folder not found: {candidate}")
    return candidate


def _resolve_optional_file(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ImageOcrError(f"OCR prompt file not found: {candidate}")
    return candidate


def discover_images(folder: Path) -> list[Path]:
    return discover_source_images(folder)


def _validate_output_collisions(images: list[Path], root: Path) -> None:
    try:
        validate_image_output_collisions(images, root)
    except SourceRegistrationError as error:
        raise ImageOcrError(str(error)) from error


def _load_image(path: Path) -> Image.Image:
    with Image.open(path) as opened_image:
        return ImageOps.exif_transpose(opened_image).convert("RGB").copy()


def _read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _load_cached_result(
    path: Path,
    *,
    image_sha256: str,
    common_prompt_sha256: str,
    image_prompt_sha256: str,
    provider: ImageOcrProvider,
) -> dict[str, Any] | None:
    value = read_json_object(path)
    if value is None:
        return None
    try:
        matches = (
            value.get("image_sha256") == image_sha256
            and value.get("common_prompt_sha256") == common_prompt_sha256
            and value.get("image_prompt_sha256") == image_prompt_sha256
            and value.get("model") == provider.model_name
            and value.get("prompt_version") == provider.prompt_version
        )
        if not matches:
            return None
        return validate_ocr_result(value["ocr"])
    except (KeyError, TypeError, ValueError) as error:
        raise invalid_cache(path, "invalid OCR result") from error


def _registered_source_folder(location: ProjectLocation) -> Path:
    if location.manifest.source_file != IMAGE_SOURCE_ROOT:
        if location.manifest.source_file:
            raise ImageOcrError(
                f"Project source is already registered as {location.manifest.source_file}. "
                "Provide --folder with --force to replace the project source type."
            )
        raise ImageOcrError("No image folder is registered; provide --folder.")
    return WorkspacePaths(location.path).input_images_dir


def _resolve_ocr_request(
    *,
    location: ProjectLocation,
    folder: str | Path | None,
    prompt_file: str | Path | None,
) -> tuple[Path, list[Path], Path | None]:
    source_folder = (
        _registered_source_folder(location)
        if folder is None
        else _resolve_folder(folder)
    )
    images = discover_images(source_folder)
    if not images:
        raise ImageOcrError(f"No supported images found in {source_folder}")
    _validate_output_collisions(images, source_folder)

    requested_prompt = _resolve_optional_file(prompt_file) if prompt_file else None
    if requested_prompt is None:
        folder_prompt = source_folder / "ocr_prompt.txt"
        requested_prompt = folder_prompt if folder_prompt.is_file() else None
    if requested_prompt is None:
        saved_prompt = WorkspacePaths(location.path).input_ocr_prompt
        requested_prompt = saved_prompt if saved_prompt.is_file() else None
    return source_folder, images, requested_prompt


def _prepare_registered_ocr_input(
    *,
    location: ProjectLocation,
    source_folder: Path,
    images: list[Path],
    requested_prompt: Path | None,
    register_source: bool,
    force: bool,
) -> _RegisteredOcrInput:
    if register_source:
        try:
            registered = register_image_sources(
                location,
                source_folder,
                images,
                force=force,
            )
        except SourceRegistrationError as error:
            raise ImageOcrError(str(error)) from error
        location = registered.location
        registered_folder = registered.root
        registered_images = registered.files
    else:
        registered_folder = source_folder
        registered_images = tuple(images)

    prompt_destination = WorkspacePaths(location.path).input_ocr_prompt
    registered_prompt: Path | None = None
    if requested_prompt is not None:
        registered_prompt = prompt_destination
        if requested_prompt.resolve() != registered_prompt.resolve():
            _copy_file_atomic(requested_prompt, registered_prompt)
    elif prompt_destination.is_file():
        registered_prompt = prompt_destination
    return _RegisteredOcrInput(
        location,
        registered_folder,
        registered_images,
        registered_prompt,
    )


def _ocr_image(
    *,
    image_path: Path,
    registered: _RegisteredOcrInput,
    paths: WorkspacePaths,
    provider: ImageOcrProvider,
    common_instructions: str,
    common_prompt_hash: str,
    force: bool,
    notify: ProgressCallback,
    progress_label: str,
) -> _ImageOcrOutput:
    relative = image_path.relative_to(registered.folder)
    relative_name = relative.as_posix()
    text_relative = relative.with_suffix(".txt")
    result_path = paths.ocr_results / relative.with_suffix(".json")
    individual_path = paths.ocr_individual / text_relative
    image_prompt_path = image_path.with_name(image_path.name + ".prompt.txt")
    image_instructions = _read_text(image_prompt_path)
    image_hash = _sha256_file(image_path)
    image_prompt_hash = _sha256_text(image_instructions)

    ocr = None if force else _load_cached_result(
        result_path,
        image_sha256=image_hash,
        common_prompt_sha256=common_prompt_hash,
        image_prompt_sha256=image_prompt_hash,
        provider=provider,
    )
    cached = ocr is not None
    if cached:
        notify(f"{progress_label}: reused validated OCR cache")
    else:
        prompt = build_ocr_prompt(common_instructions, image_instructions)
        ocr = validate_ocr_result(
            provider.transcribe(prompt, _load_image(image_path))
        )
        _write_json_atomic(
            result_path,
            {
                "schema_version": 1,
                "source_image": f"{IMAGE_SOURCE_ROOT}/{relative_name}",
                "image_sha256": image_hash,
                "common_prompt_file": (
                    paths.relative(paths.input_ocr_prompt)
                    if registered.prompt
                    else None
                ),
                "common_prompt_sha256": common_prompt_hash,
                "image_prompt_file": (
                    f"{IMAGE_SOURCE_ROOT}/{relative_name}.prompt.txt"
                    if image_prompt_path.is_file()
                    else None
                ),
                "image_prompt_sha256": image_prompt_hash,
                "model": provider.model_name,
                "prompt_version": provider.prompt_version,
                "ocr": ocr,
                "updated_at": _utc_now(),
            },
        )
    assert ocr is not None
    text = build_individual_text(ocr["blocks"])
    _write_text_atomic(individual_path, text)
    return _ImageOcrOutput(
        relative_name,
        text_relative.as_posix(),
        text,
        cached,
        ocr["status"] == "needs_review",
    )


def _preserve_previous_ocr_text(
    path: Path,
    failure_message: str,
) -> tuple[str, str]:
    try:
        return path.read_text(encoding="utf-8"), failure_message
    except FileNotFoundError:
        return "", failure_message
    except OSError as error:
        return (
            "",
            failure_message
            + f"; could not preserve existing OCR text: {error}",
        )


def _ocr_registered_images(
    *,
    registered: _RegisteredOcrInput,
    paths: WorkspacePaths,
    provider: ImageOcrProvider,
    common_instructions: str,
    common_prompt_hash: str,
    force: bool,
    notify: ProgressCallback,
) -> _ImageOcrBatch:
    successful: list[_ImageOcrOutput] = []
    combined_items: list[tuple[str, str]] = []
    failures: list[ImageOcrFailure] = []
    total = len(registered.images)
    for index, image_path in enumerate(registered.images, start=1):
        relative = image_path.relative_to(registered.folder)
        source_name = relative.as_posix()
        text_name = relative.with_suffix(".txt").as_posix()
        progress_label = f"Image {index}/{total}: {source_name}"
        notify(progress_label)
        try:
            output = _ocr_image(
                image_path=image_path,
                registered=registered,
                paths=paths,
                provider=provider,
                common_instructions=common_instructions,
                common_prompt_hash=common_prompt_hash,
                force=force,
                notify=notify,
                progress_label=progress_label,
            )
            successful.append(output)
            combined_items.append((output.text_name, output.text))
        except ProgressCallbackError:
            raise
        except Exception as error:
            previous_text, failure_message = _preserve_previous_ocr_text(
                paths.ocr_individual / relative.with_suffix(".txt"),
                str(error),
            )
            failures.append(
                ImageOcrFailure(
                    source_name,
                    failure_message,
                    ai_failure_code(error),
                )
            )
            combined_items.append((text_name, previous_text))
            notify(f"{progress_label}: failed: {error}")
    return _ImageOcrBatch(
        tuple(successful),
        tuple(combined_items),
        tuple(failures),
    )


def _write_ocr_result(
    *,
    registered: _RegisteredOcrInput,
    paths: WorkspacePaths,
    provider: ImageOcrProvider,
    batch: _ImageOcrBatch,
) -> Path:
    combined_path = (
        paths.ocr_combined_partial
        if batch.failures
        else paths.ocr_combined
    )
    _write_text_atomic(
        combined_path,
        build_combined_text(list(batch.combined_items)),
    )
    _write_json_atomic(
        paths.image_ocr_state,
        {
            "schema_version": 1,
            "status": "partial" if batch.failures else "complete",
            "source_folder": IMAGE_SOURCE_ROOT,
            "prompt_file": (
                paths.relative(paths.input_ocr_prompt)
                if registered.prompt
                else None
            ),
            "model": provider.model_name,
            "prompt_version": provider.prompt_version,
            "total_images": len(registered.images),
            "successful_images": [
                item.source_name for item in batch.successful
            ],
            "cached_images": [
                item.source_name for item in batch.successful if item.cached
            ],
            "needs_review": [
                item.source_name
                for item in batch.successful
                if item.needs_review
            ],
            "failures": [asdict(failure) for failure in batch.failures],
            "output_file": str(combined_path.relative_to(registered.location.path)),
            "updated_at": _utc_now(),
        },
    )
    return combined_path


def ocr_project_images(
    *,
    project: str | Path,
    folder: str | Path | None = None,
    prompt_file: str | Path | None = None,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    model_name: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    provider: ImageOcrProvider | None = None,
    progress: ProgressCallback | None = None,
) -> ImageOcrRunResult:
    notify = guard_progress_callback(progress)
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    source_folder, images, requested_prompt = _resolve_ocr_request(
        location=location,
        folder=folder,
        prompt_file=prompt_file,
    )
    selected = tuple(path.relative_to(source_folder).as_posix() for path in images)
    if dry_run:
        return ImageOcrRunResult(
            project_path=str(location.path),
            source_folder=str(source_folder),
            prompt_file=str(requested_prompt) if requested_prompt else None,
            model=model_name,
            prompt_version=None,
            selected_images=selected,
            successful_images=(),
            cached_images=(),
            needs_review=(),
            failures=(),
            output_file=None,
            dry_run=True,
        )

    registered = _prepare_registered_ocr_input(
        location=location,
        source_folder=source_folder,
        images=images,
        requested_prompt=requested_prompt,
        register_source=folder is not None,
        force=force,
    )
    paths = WorkspacePaths(registered.location.path)
    common_instructions = _read_text(registered.prompt)
    common_prompt_hash = _sha256_text(common_instructions)
    active_provider = provider or create_image_ocr_provider(
        model_name,
        settings_root=settings_root,
    )
    batch = _ocr_registered_images(
        registered=registered,
        paths=paths,
        provider=active_provider,
        common_instructions=common_instructions,
        common_prompt_hash=common_prompt_hash,
        force=force,
        notify=notify,
    )
    combined_path = _write_ocr_result(
        registered=registered,
        paths=paths,
        provider=active_provider,
        batch=batch,
    )
    return ImageOcrRunResult(
        project_path=str(registered.location.path),
        source_folder=str(registered.folder),
        prompt_file=str(registered.prompt) if registered.prompt else None,
        model=active_provider.model_name,
        prompt_version=active_provider.prompt_version,
        selected_images=selected,
        successful_images=tuple(
            item.source_name for item in batch.successful
        ),
        cached_images=tuple(
            item.source_name for item in batch.successful if item.cached
        ),
        needs_review=tuple(
            item.source_name
            for item in batch.successful
            if item.needs_review
        ),
        failures=batch.failures,
        output_file=str(combined_path),
        usage=provider_usage(active_provider),
    )
