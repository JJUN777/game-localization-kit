"""Project-level image registration and structured OCR orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image, ImageOps

from glk.application._cache import invalid_cache, read_json_object
from glk.application._hashing import sha256_file as _sha256_file
from glk.application._hashing import sha256_text as _sha256_text
from glk.application._io import copy_file_atomic as _copy_file_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application._io import write_text_atomic as _write_text_atomic
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
from glk.infrastructure.gemini_ocr import GeminiImageOcrProvider
from glk.infrastructure.gemini_common import gemini_failure_code


IMAGE_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS
ProgressCallback = Callable[[str], None]


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
    notify = progress or (lambda _: None)
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    if folder is None:
        source_folder = _registered_source_folder(location)
    else:
        source_folder = _resolve_folder(folder)
    images = discover_images(source_folder)
    if not images:
        raise ImageOcrError(f"No supported images found in {source_folder}")
    _validate_output_collisions(images, source_folder)
    selected = tuple(path.relative_to(source_folder).as_posix() for path in images)

    requested_prompt = _resolve_optional_file(prompt_file) if prompt_file else None
    if requested_prompt is None:
        folder_prompt = source_folder / "ocr_prompt.txt"
        requested_prompt = folder_prompt if folder_prompt.is_file() else None
    if requested_prompt is None:
        saved_prompt = paths.input_ocr_prompt
        requested_prompt = saved_prompt if saved_prompt.is_file() else None

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

    if folder is not None:
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
        registered_images = list(registered.files)
    else:
        registered_folder = source_folder
        registered_images = images

    registered_prompt: Path | None = None
    if requested_prompt is not None:
        registered_prompt = paths.input_ocr_prompt
        if requested_prompt.resolve() != registered_prompt.resolve():
            _copy_file_atomic(requested_prompt, registered_prompt)
    elif paths.input_ocr_prompt.is_file():
        registered_prompt = paths.input_ocr_prompt

    common_instructions = _read_text(registered_prompt)
    common_prompt_hash = _sha256_text(common_instructions)
    active_provider = provider or GeminiImageOcrProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )
    individual_dir = paths.ocr_individual
    results_dir = paths.ocr_results
    combined_items: list[tuple[str, str]] = []
    successful: list[str] = []
    cached_images: list[str] = []
    needs_review: list[str] = []
    failures: list[ImageOcrFailure] = []

    for index, image_path in enumerate(registered_images, start=1):
        relative = image_path.relative_to(registered_folder)
        relative_name = relative.as_posix()
        text_relative = relative.with_suffix(".txt")
        result_path = results_dir / relative.with_suffix(".json")
        individual_path = individual_dir / text_relative
        image_prompt_path = image_path.with_name(image_path.name + ".prompt.txt")
        image_instructions = _read_text(image_prompt_path)
        image_hash = _sha256_file(image_path)
        image_prompt_hash = _sha256_text(image_instructions)
        notify(f"Image {index}/{len(registered_images)}: {relative_name}")
        try:
            ocr = None if force else _load_cached_result(
                result_path,
                image_sha256=image_hash,
                common_prompt_sha256=common_prompt_hash,
                image_prompt_sha256=image_prompt_hash,
                provider=active_provider,
            )
            if ocr is not None:
                cached_images.append(relative_name)
                notify(f"Image {index}/{len(registered_images)}: reused validated OCR cache")
            else:
                prompt = build_ocr_prompt(common_instructions, image_instructions)
                ocr = validate_ocr_result(
                    active_provider.transcribe(prompt, _load_image(image_path))
                )
                _write_json_atomic(
                    result_path,
                    {
                        "schema_version": 1,
                        "source_image": f"{IMAGE_SOURCE_ROOT}/{relative.as_posix()}",
                        "image_sha256": image_hash,
                        "common_prompt_file": (
                            paths.relative(paths.input_ocr_prompt)
                            if registered_prompt
                            else None
                        ),
                        "common_prompt_sha256": common_prompt_hash,
                        "image_prompt_file": (
                            f"{IMAGE_SOURCE_ROOT}/{relative_name}.prompt.txt"
                            if image_prompt_path.is_file()
                            else None
                        ),
                        "image_prompt_sha256": image_prompt_hash,
                        "model": active_provider.model_name,
                        "prompt_version": active_provider.prompt_version,
                        "ocr": ocr,
                        "updated_at": _utc_now(),
                    },
                )
            text = build_individual_text(ocr["blocks"])
            _write_text_atomic(individual_path, text)
            combined_items.append((text_relative.as_posix(), text))
            successful.append(relative_name)
            if ocr["status"] == "needs_review":
                needs_review.append(relative_name)
        except Exception as error:
            failure_message = str(error)
            try:
                previous_text = individual_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                previous_text = ""
            except OSError as previous_error:
                previous_text = ""
                failure_message += (
                    f"; could not preserve existing OCR text: {previous_error}"
                )
            failures.append(
                ImageOcrFailure(
                    relative_name,
                    failure_message,
                    gemini_failure_code(error),
                )
            )
            combined_items.append((text_relative.as_posix(), previous_text))
            notify(f"Image {index}/{len(registered_images)}: failed: {error}")

    combined_path = (
        paths.ocr_combined_partial if failures else paths.ocr_combined
    )
    _write_text_atomic(combined_path, build_combined_text(combined_items))
    run_status = {
        "schema_version": 1,
        "status": "partial" if failures else "complete",
        "source_folder": IMAGE_SOURCE_ROOT,
        "prompt_file": (
            paths.relative(paths.input_ocr_prompt) if registered_prompt else None
        ),
        "model": active_provider.model_name,
        "prompt_version": active_provider.prompt_version,
        "total_images": len(registered_images),
        "successful_images": successful,
        "cached_images": cached_images,
        "needs_review": needs_review,
        "failures": [asdict(failure) for failure in failures],
        "output_file": str(combined_path.relative_to(location.path)),
        "updated_at": _utc_now(),
    }
    _write_json_atomic(paths.image_ocr_state, run_status)
    return ImageOcrRunResult(
        project_path=str(location.path),
        source_folder=str(registered_folder),
        prompt_file=str(registered_prompt) if registered_prompt else None,
        model=active_provider.model_name,
        prompt_version=active_provider.prompt_version,
        selected_images=selected,
        successful_images=tuple(successful),
        cached_images=tuple(cached_images),
        needs_review=tuple(needs_review),
        failures=tuple(failures),
        output_file=str(combined_path),
    )
