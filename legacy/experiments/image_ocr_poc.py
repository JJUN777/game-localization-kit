"""Image-folder OCR proof of concept with prompt layering and deterministic outputs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

from google.genai import types
from PIL import Image, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(LEGACY_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LEGACY_SCRIPTS))
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from common import call_gemini_with_retry, init_pipeline  # noqa: E402
from glk.extraction.image_ocr import (  # noqa: E402
    OCR_PROMPT_VERSION,
    OCR_RESPONSE_SCHEMA,
    build_combined_text,
    build_individual_text,
    build_ocr_prompt,
    validate_ocr_result,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_path(raw_path: str, *, must_exist: bool = True) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    return path


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_optional_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


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
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    _write_bytes_atomic(path, data.encode("utf-8"))


def _output_relative_path(image_path: Path, input_folder: Path, suffix: str) -> Path:
    return image_path.relative_to(input_folder).with_suffix(suffix)


def validate_output_collisions(images: list[Path], input_folder: Path) -> None:
    outputs: dict[Path, Path] = {}
    for image_path in images:
        output_path = _output_relative_path(image_path, input_folder, ".txt")
        previous = outputs.get(output_path)
        if previous is not None:
            raise ValueError(
                f"Output filename collision: {previous} and {image_path} both map to {output_path}"
            )
        outputs[output_path] = image_path


def _load_image(path: Path) -> Image.Image:
    with Image.open(path) as opened_image:
        return ImageOps.exif_transpose(opened_image).convert("RGB").copy()


def request_ocr(
    client: Any,
    model_name: str,
    prompt: str,
    image: Image.Image,
) -> dict[str, Any]:
    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_json_schema=OCR_RESPONSE_SCHEMA,
    )
    response = call_gemini_with_retry(
        client,
        model_name,
        [prompt, image],
        generation_config=config,
    )
    if not response.text:
        raise ValueError("Gemini returned an empty OCR response.")
    value = json.loads(response.text)
    return validate_ocr_result(value)


def load_cached_result(
    result_path: Path,
    *,
    image_sha256: str,
    common_prompt_sha256: str,
    image_prompt_sha256: str,
    model_name: str,
) -> dict[str, Any] | None:
    if not result_path.is_file():
        return None
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
        matches = (
            value.get("image_sha256") == image_sha256
            and value.get("common_prompt_sha256") == common_prompt_sha256
            and value.get("image_prompt_sha256") == image_prompt_sha256
            and value.get("model") == model_name
            and value.get("prompt_version") == OCR_PROMPT_VERSION
        )
        if not matches:
            return None
        validated = validate_ocr_result(value["ocr"])
        return {**value, "ocr": validated}
    except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def run(args: argparse.Namespace) -> int:
    input_folder = _resolve_path(args.folder)
    if not input_folder.is_dir():
        raise NotADirectoryError(f"Image folder not found: {input_folder}")
    output_dir = _resolve_path(args.output_dir, must_exist=False)
    prompt_path = (
        _resolve_path(args.prompt)
        if args.prompt
        else input_folder / "ocr_prompt.txt"
    )
    common_instructions = _read_optional_text(prompt_path)
    common_prompt_sha256 = _sha256_bytes(common_instructions.encode("utf-8"))
    images = discover_images(input_folder)
    if not images:
        raise ValueError(f"No supported images found in {input_folder}")
    validate_output_collisions(images, input_folder)
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be greater than zero")
        images = images[: args.limit]
    config, client = init_pipeline()
    if client is None or not config:
        return 2
    model_name = args.model or config.get("translation", {}).get("model_name")
    if not model_name:
        raise ValueError("No Gemini model is configured.")

    individual_dir = output_dir / "individual"
    results_dir = output_dir / "results"
    combined_items: list[tuple[str, str]] = []
    successes: list[str] = []
    cached_files: list[str] = []
    needs_review: list[str] = []
    failures: list[dict[str, str]] = []
    logging.info(
        "Image OCR PoC: %d images from %s; one target image per request",
        len(images),
        input_folder,
    )

    for index, image_path in enumerate(images, start=1):
        relative_image = image_path.relative_to(input_folder)
        relative_text = _output_relative_path(image_path, input_folder, ".txt")
        result_relative = _output_relative_path(image_path, input_folder, ".json")
        result_path = results_dir / result_relative
        individual_path = individual_dir / relative_text
        image_prompt_path = image_path.with_name(image_path.name + ".prompt.txt")
        image_instructions = _read_optional_text(image_prompt_path)
        image_sha256 = _sha256_file(image_path)
        image_prompt_sha256 = _sha256_bytes(image_instructions.encode("utf-8"))
        logging.info("[%d/%d] %s", index, len(images), relative_image.as_posix())
        try:
            cached = None if args.force else load_cached_result(
                result_path,
                image_sha256=image_sha256,
                common_prompt_sha256=common_prompt_sha256,
                image_prompt_sha256=image_prompt_sha256,
                model_name=model_name,
            )
            if cached is not None:
                ocr = cached["ocr"]
                cached_files.append(relative_image.as_posix())
                logging.info("Reused validated OCR cache: %s", relative_image)
            else:
                image = _load_image(image_path)
                prompt = build_ocr_prompt(
                    common_instructions,
                    image_instructions,
                )
                ocr = request_ocr(
                    client,
                    model_name,
                    prompt,
                    image,
                )
                _write_json_atomic(
                    result_path,
                    {
                        "schema_version": 1,
                        "source_image": relative_image.as_posix(),
                        "image_sha256": image_sha256,
                        "common_prompt_file": (
                            prompt_path.relative_to(input_folder).as_posix()
                            if prompt_path.is_relative_to(input_folder)
                            else str(prompt_path)
                        ),
                        "common_prompt_sha256": common_prompt_sha256,
                        "image_prompt_file": (
                            image_prompt_path.relative_to(input_folder).as_posix()
                            if image_prompt_path.is_file()
                            else None
                        ),
                        "image_prompt_sha256": image_prompt_sha256,
                        "model": model_name,
                        "prompt_version": OCR_PROMPT_VERSION,
                        "ocr": ocr,
                        "updated_at": _utc_now(),
                    },
                )
            text = build_individual_text(ocr["blocks"])
            _write_text_atomic(individual_path, text)
            combined_items.append((relative_text.as_posix(), text))
            successes.append(relative_image.as_posix())
            if ocr["status"] == "needs_review":
                needs_review.append(relative_image.as_posix())
        except Exception as error:
            logging.exception("OCR failed for %s: %s", relative_image, error)
            failures.append({"file": relative_image.as_posix(), "error": str(error)})
            _write_text_atomic(individual_path, "")
            combined_items.append((relative_text.as_posix(), ""))

    combined_name = "combined.partial.txt" if failures else "combined.txt"
    combined_path = output_dir / combined_name
    _write_text_atomic(combined_path, build_combined_text(combined_items))
    summary = {
        "schema_version": 1,
        "status": "partial" if failures else "complete",
        "input_folder": str(input_folder),
        "output_file": str(combined_path),
        "model": model_name,
        "prompt_version": OCR_PROMPT_VERSION,
        "total_images": len(images),
        "successful_files": successes,
        "cached_files": cached_files,
        "needs_review": needs_review,
        "failures": failures,
        "updated_at": _utc_now(),
    }
    _write_json_atomic(output_dir / "run_summary.json", summary)
    logging.info(
        "OCR completed: %d/%d successful, %d cached, %d need review, %d failed",
        len(successes),
        len(images),
        len(cached_files),
        len(needs_review),
        len(failures),
    )
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PoC: OCR an image folder with prompt layering.")
    parser.add_argument(
        "--folder",
        default="legacy/samples/image_ocr",
        help="Input image folder",
    )
    parser.add_argument("--output-dir", default="97_image_ocr_poc", help="PoC output directory")
    parser.add_argument("--prompt", help="Project-wide additional OCR instructions")
    parser.add_argument("--model", help="Gemini model override")
    parser.add_argument("--limit", type=int, help="Process only the first N images")
    parser.add_argument("--force", action="store_true", help="Ignore OCR cache")
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except Exception as error:
        logging.exception("Image OCR PoC failed: %s", error)
        raise SystemExit(2) from error
