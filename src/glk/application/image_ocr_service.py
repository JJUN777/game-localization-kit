"""Project-level image registration and structured OCR orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Protocol

from PIL import Image, ImageOps

from glk.application.project_service import (
    ProjectLocation,
    load_project,
    update_project_source,
)
from glk.extraction.image_ocr import (
    build_combined_text,
    build_individual_text,
    build_ocr_prompt,
    validate_ocr_result,
)
from glk.infrastructure.gemini_ocr import GeminiImageOcrProvider


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
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


def _natural_key(path: Path, root: Path) -> list[tuple[int, int | str]]:
    relative = path.relative_to(root).as_posix().casefold()
    return [
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", relative)
        if part
    ]


def discover_images(folder: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(folder).parts)
            and path.suffix.casefold() in IMAGE_EXTENSIONS
        ),
        key=lambda path: _natural_key(path, folder),
    )


def _validate_output_collisions(images: list[Path], root: Path) -> None:
    outputs: dict[Path, Path] = {}
    for image_path in images:
        output = image_path.relative_to(root).with_suffix(".txt")
        previous = outputs.get(output)
        if previous is not None:
            raise ImageOcrError(
                f"Output filename collision: {previous.name} and {image_path.name} "
                f"both map to {output.as_posix()}"
            )
        outputs[output] = image_path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _write_text_atomic(path: Path, value: str) -> None:
    text = value if not value or value.endswith("\n") else value + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temporary_path)
    with temporary_path.open("rb+") as file:
        os.fsync(file.fileno())
    os.replace(temporary_path, destination)


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
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
    except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _registered_source_folder(location: ProjectLocation) -> Path:
    if location.manifest.source_file != "source/images":
        if location.manifest.source_file:
            raise ImageOcrError(
                f"Project source is already registered as {location.manifest.source_file}. "
                "Provide --folder with --force to replace the project source type."
            )
        raise ImageOcrError("No image folder is registered; provide --folder.")
    return location.path / "source/images"


def _register_images(
    location: ProjectLocation,
    source_folder: Path,
    images: list[Path],
    *,
    force: bool,
) -> tuple[ProjectLocation, Path, list[Path]]:
    if location.manifest.source_file not in {None, "source/images"} and not force:
        raise ImageOcrError(
            f"Project source is already registered as {location.manifest.source_file}. "
            "Use --force to replace the project source type."
        )
    destination_root = location.path / "source/images"
    registered: list[Path] = []
    for source_image in images:
        relative = source_image.relative_to(source_folder)
        destination = destination_root / relative
        if source_image.resolve() != destination.resolve():
            if destination.is_file():
                same = _sha256_file(source_image) == _sha256_file(destination)
                if not same and not force:
                    raise ImageOcrError(
                        f"A different source image is already registered: {relative}. "
                        "Use --force to replace it."
                    )
                if not same:
                    _copy_file_atomic(source_image, destination)
            else:
                _copy_file_atomic(source_image, destination)
        registered.append(destination)

        sidecar = source_image.with_name(source_image.name + ".prompt.txt")
        if sidecar.is_file():
            destination_sidecar = destination.with_name(destination.name + ".prompt.txt")
            if sidecar.resolve() != destination_sidecar.resolve():
                _copy_file_atomic(sidecar, destination_sidecar)
    if location.manifest.source_file != "source/images":
        location = update_project_source(location, "source/images")
    return location, destination_root, registered


def ocr_project_images(
    *,
    project: str | Path,
    folder: str | Path | None = None,
    prompt_file: str | Path | None = None,
    workspace_root: str | Path = "workspaces",
    model_name: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    provider: ImageOcrProvider | None = None,
    progress: ProgressCallback | None = None,
) -> ImageOcrRunResult:
    notify = progress or (lambda _: None)
    location = load_project(project, workspace_root)
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
        registered_prompt = location.path / "source/ocr_prompt.txt"
        requested_prompt = registered_prompt if registered_prompt.is_file() else None

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
        location, registered_folder, registered_images = _register_images(
            location, source_folder, images, force=force
        )
    else:
        registered_folder = source_folder
        registered_images = images

    registered_prompt: Path | None = None
    if requested_prompt is not None:
        registered_prompt = location.path / "source/ocr_prompt.txt"
        if requested_prompt.resolve() != registered_prompt.resolve():
            _copy_file_atomic(requested_prompt, registered_prompt)
    elif (location.path / "source/ocr_prompt.txt").is_file():
        registered_prompt = location.path / "source/ocr_prompt.txt"

    common_instructions = _read_text(registered_prompt)
    common_prompt_hash = _sha256_bytes(common_instructions.encode("utf-8"))
    active_provider = provider or GeminiImageOcrProvider.from_environment(model_name)
    output_root = location.path / "source/ocr"
    individual_dir = output_root / "individual"
    results_dir = output_root / "results"
    state_dir = location.path / "state"
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
        image_prompt_hash = _sha256_bytes(image_instructions.encode("utf-8"))
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
                        "source_image": f"source/images/{relative.as_posix()}",
                        "image_sha256": image_hash,
                        "common_prompt_file": (
                            "source/ocr_prompt.txt" if registered_prompt else None
                        ),
                        "common_prompt_sha256": common_prompt_hash,
                        "image_prompt_file": (
                            f"source/images/{relative_name}.prompt.txt"
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
            failures.append(ImageOcrFailure(relative_name, str(error)))
            _write_text_atomic(individual_path, "")
            combined_items.append((text_relative.as_posix(), ""))
            notify(f"Image {index}/{len(registered_images)}: failed: {error}")

    combined_name = "combined.partial.txt" if failures else "combined.txt"
    combined_path = output_root / combined_name
    _write_text_atomic(combined_path, build_combined_text(combined_items))
    run_status = {
        "schema_version": 1,
        "status": "partial" if failures else "complete",
        "source_folder": "source/images",
        "prompt_file": "source/ocr_prompt.txt" if registered_prompt else None,
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
    _write_json_atomic(output_root / "run_summary.json", run_status)
    _write_json_atomic(state_dir / "image_ocr.json", run_status)
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
