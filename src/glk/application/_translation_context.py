"""Load and validate the shared inputs for translation operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from glk.application.translation_types import (
    DEFAULT_PROJECT_INSTRUCTIONS,
    TranslationError,
)
from glk.domain.source_block import SourceBlock, SourceBlockValidationError
from glk.domain.workspace import WorkspacePaths


def load_approved_blocks(project_path: Path) -> tuple[list[SourceBlock], bytes]:
    path = WorkspacePaths(project_path).approved_source_segments
    if not path.is_file():
        raise TranslationError(
            f"Final common source not found: {path}. Run glk review finalize first."
        )
    data = path.read_bytes()
    blocks: list[SourceBlock] = []
    line_number = 0
    try:
        for line_number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            block = SourceBlock.from_dict(json.loads(line))
            if block.status != "approved":
                raise TranslationError(
                    f"Approved source contains non-approved block {block.id}."
                )
            blocks.append(block)
    except TranslationError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceBlockValidationError,
        TypeError,
    ) as error:
        raise TranslationError(
            f"Invalid approved source JSONL at line {line_number}: {error}"
        ) from error
    if not blocks:
        raise TranslationError("Final common source is empty.")
    if len({block.id for block in blocks}) != len(blocks):
        raise TranslationError("Final common source contains duplicate block IDs.")
    return sorted(blocks, key=lambda block: block.source_order), data


def load_termbase(project_path: Path) -> tuple[list[dict[str, Any]], bytes]:
    path = WorkspacePaths(project_path).termbase
    if not path.is_file():
        raise TranslationError(
            f"Current termbase not found: {path}. Run glk glossary import first."
        )
    data = path.read_bytes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranslationError(f"Invalid termbase JSON: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise TranslationError("Termbase must contain an entries array.")
    active: list[dict[str, Any]] = []
    for index, entry in enumerate(value["entries"], start=1):
        if not isinstance(entry, dict):
            raise TranslationError(f"Termbase entry {index} is not an object.")
        status = entry.get("status")
        if status not in {"approved", "keep", "rejected"}:
            raise TranslationError(
                f"Termbase entry {index} has invalid status {status!r}."
            )
        if status == "rejected":
            continue
        source_term = entry.get("source_term")
        translation = entry.get("translation")
        variants = entry.get("variants")
        if (
            not isinstance(source_term, str)
            or not source_term.strip()
            or not isinstance(translation, str)
            or not translation.strip()
            or not isinstance(variants, list)
            or not all(isinstance(item, str) and item for item in variants)
        ):
            raise TranslationError(f"Termbase entry {index} is incomplete.")
        active.append(entry)
    return active, data


def resolve_translation_prompt(
    prompt_file: str | Path | None, project_path: Path
) -> tuple[str, Path | None, bool]:
    canonical_path = WorkspacePaths(project_path).translation_prompt
    if prompt_file is None:
        if canonical_path.is_file():
            try:
                return canonical_path.read_text(encoding="utf-8"), canonical_path, False
            except UnicodeDecodeError as error:
                raise TranslationError(
                    f"Translation prompt must be UTF-8: {canonical_path}"
                ) from error
        return DEFAULT_PROJECT_INSTRUCTIONS, canonical_path, True

    requested = Path(prompt_file).expanduser()
    candidates = (
        (requested.resolve(),)
        if requested.is_absolute()
        else ((project_path / requested).resolve(), (Path.cwd() / requested).resolve())
    )
    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        raise TranslationError(
            "Translation prompt not found. Checked "
            + " and ".join(str(path) for path in candidates)
            + "."
        )
    try:
        text = selected.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise TranslationError(f"Translation prompt must be UTF-8: {selected}") from error
    if not text.strip():
        raise TranslationError("Translation prompt cannot be empty.")
    return text, canonical_path, selected.resolve() != canonical_path.resolve()
