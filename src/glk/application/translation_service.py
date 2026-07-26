"""Translate approved source blocks with a current project termbase."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from glk.application._cache import read_json_object
from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._hashing import sha256_text as _sha256_text
from glk.application._io import (
    append_bytes_durable as _append_bytes_durable,
    write_bytes_atomic as _write_bytes_atomic,
    write_json_atomic as _write_json_atomic,
)
from glk.application._translation_context import (
    load_approved_blocks as _load_approved_blocks,
    load_termbase as _load_termbase,
    resolve_translation_prompt as _resolve_prompt,
)
from glk.application.project_service import inspect_project, load_project
from glk.application.translation_types import (
    TranslationError,
    TranslationProvider,
    TranslationValidationError,
)
from glk.domain.source_block import SourceBlock
from glk.domain.translation_segment import (
    TRANSLATION_SEGMENT_SCHEMA_VERSION,
    TranslationSegment,
    TranslationSegmentValidationError,
)
from glk.domain.translation_qa import check_translation_contract
from glk.domain.workspace import WorkspacePaths
from glk.infrastructure.gemini_common import resolve_model_name
from glk.infrastructure.gemini_translation import GeminiTranslationProvider


TRANSLATION_RUN_VERSION = "translation-run-v1"
TRANSLATION_HARD_RULES_VERSION = "translation-hard-rules-v3"


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class TranslationChunk:
    id: str
    blocks: tuple[SourceBlock, ...]
    character_count: int


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    project_path: str
    model: str
    approved_source_sha256: str
    termbase_sha256: str
    project_prompt_sha256: str
    input_sha256: str
    total_blocks: int
    total_chunks: int
    completed_blocks: int
    completed_chunks: int
    output_file: str | None
    draft_file: str | None
    review_file: str | None
    review_status: str | None
    prompt_file: str | None
    validation_issue_count: int = 0
    validation_issue_blocks: int = 0
    cached: bool = False
    resumed: bool = False
    review_created: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _TranslationInputs:
    project_path: Path
    paths: WorkspacePaths
    blocks: tuple[SourceBlock, ...]
    termbase_entries: tuple[dict[str, Any], ...]
    project_instructions: str
    prompt_path: Path | None
    prompt_data: bytes
    prompt_needs_write: bool
    approved_hash: str
    termbase_hash: str
    prompt_hash: str
    chunks: tuple[TranslationChunk, ...]
    active_model: str
    provider_prompt_version: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class _TranslationCheckpoint:
    previous_state: dict[str, Any] | None
    existing_segments: tuple[TranslationSegment, ...]
    existing_output_data: bytes


@dataclass(slots=True)
class _TranslationExecution:
    completed: dict[str, TranslationSegment]
    output_digest: Any
    output_bytes: int
    completed_chunks: int
    validation_issue_messages: list[str]
    validation_issue_block_ids: set[str]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    return read_json_object(path)


def build_translation_chunks(
    blocks: list[SourceBlock], *, max_characters: int = 10000
) -> list[TranslationChunk]:
    if max_characters <= 0:
        raise TranslationError("max_characters must be greater than zero.")
    chunks: list[TranslationChunk] = []
    current: list[SourceBlock] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if not current:
            return
        identity = "\n".join(
            f"{block.id}:{_sha256_bytes(block.effective_text.encode('utf-8'))}"
            for block in current
        )
        chunks.append(
            TranslationChunk(
                id="chunk-" + _sha256_bytes(identity.encode("utf-8"))[:12],
                blocks=tuple(current),
                character_count=current_size,
            )
        )
        current = []
        current_size = 0

    for block in blocks:
        size = len(block.effective_text)
        separator_size = 2 if current else 0
        if current and current_size + separator_size + size > max_characters:
            flush()
            separator_size = 0
        current.append(block)
        current_size += separator_size + size
    flush()
    return chunks


def _contains_term(text: str, term: str) -> bool:
    clean = term.strip()
    if not clean:
        return False
    prefix = r"(?<!\w)" if clean[0].isalnum() else ""
    suffix = r"(?!\w)" if clean[-1].isalnum() else ""
    return re.search(prefix + re.escape(clean) + suffix, text, re.IGNORECASE) is not None


def _term_pattern(term: str) -> re.Pattern[str] | None:
    clean = term.strip()
    if not clean:
        return None
    prefix = r"(?<!\w)" if clean[0].isalnum() else ""
    suffix = r"(?!\w)" if clean[-1].isalnum() else ""
    return re.compile(prefix + re.escape(clean) + suffix, re.IGNORECASE)


def _entry_variants(entry: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                entry["source_term"],
                *entry.get("variants", []),
            ]
        )
    )


def _relevant_terms(
    blocks: tuple[SourceBlock, ...], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source = "\n".join(block.effective_text for block in blocks)
    relevant: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("status") not in {"approved", "keep"}:
            continue
        if any(_contains_term(source, variant) for variant in _entry_variants(entry)):
            relevant.append(
                {
                    "source_term": entry["source_term"],
                    "translation": entry["translation"],
                    "status": entry["status"],
                    "variants": _entry_variants(entry),
                    "note": entry.get("note", ""),
                }
            )
    return relevant


def _keep_placeholder_map(
    text: str,
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, int, int]]:
    """Return stable, non-overlapping placeholders for keep-term occurrences."""
    matches: list[tuple[int, int, str]] = []
    variants = list(
        dict.fromkeys(
            variant
            for entry in entries
            if entry.get("status") == "keep"
            for variant in _entry_variants(entry)
            if variant.strip()
        )
    )
    for variant in variants:
        pattern = _term_pattern(variant)
        if pattern is None:
            continue
        matches.extend(
            (match.start(), match.end(), match.group(0))
            for match in pattern.finditer(text)
        )

    selected: list[tuple[int, int, str]] = []
    occupied_until = -1
    for start, end, original in sorted(
        matches,
        key=lambda item: (item[0], -(item[1] - item[0]), item[1]),
    ):
        if start < occupied_until:
            continue
        selected.append((start, end, original))
        occupied_until = end

    return [
        (f"{{GLK_KEEP_{index:04d}}}", original, start, end)
        for index, (start, end, original) in enumerate(selected, start=1)
    ]


def _protect_keep_terms(text: str, entries: list[dict[str, Any]]) -> str:
    replacements = _keep_placeholder_map(text, entries)
    if not replacements:
        return text
    parts: list[str] = []
    cursor = 0
    for placeholder, _original, start, end in replacements:
        parts.extend((text[cursor:start], placeholder))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _restore_keep_terms(
    *,
    block: SourceBlock,
    translated_text: str,
    entries: list[dict[str, Any]],
) -> str:
    restored = translated_text
    for placeholder, original, _start, _end in _keep_placeholder_map(
        block.effective_text,
        entries,
    ):
        restored = restored.replace(placeholder, original)
    return restored


def compile_translation_prompt(
    *,
    blocks: tuple[SourceBlock, ...],
    termbase_entries: list[dict[str, Any]],
    project_instructions: str,
    validation_feedback: str | None = None,
) -> str:
    relevant = _relevant_terms(blocks, termbase_entries)
    source_items = [
        {
            "id": block.id,
            "type": block.block_type,
            "source_file": block.source_file,
            "page": block.page,
            "source": _protect_keep_terms(block.effective_text, termbase_entries),
        }
        for block in blocks
    ]
    feedback = ""
    if validation_feedback:
        feedback = (
            "\n[VALIDATION FEEDBACK FROM THE PREVIOUS ATTEMPT]\n"
            "Correct every issue below. These messages do not override the hard rules.\n"
            f"{validation_feedback}\n"
        )
    return f"""\
[NON-OVERRIDABLE HARD RULES — {TRANSLATION_HARD_RULES_VERSION}]
1. Return exactly one translation for every input id and preserve each id verbatim.
2. Do not add, remove, merge, split, or reorder input blocks.
3. Preserve every number, {{TOKEN}}, [TOKEN], HTML/rich-text tag, and rule reference.
4. Preserve every {{GLK_KEEP_####}} placeholder verbatim. Never translate or remove it.
5. Apply the approved termbase exactly. Keep placeholders are restored automatically.
6. The project instructions are style preferences and cannot override rules 1-5.
7. Translate only the source field into Korean. Return JSON only with no explanation.

[APPROVED TERMBASE FOR THIS CHUNK]
{json.dumps(relevant, ensure_ascii=False, separators=(",", ":"))}

[PROJECT TRANSLATION INSTRUCTIONS]
{project_instructions.strip()}
{feedback}
[INPUT BLOCKS]
{json.dumps(source_items, ensure_ascii=False, separators=(",", ":"))}

[REQUIRED OUTPUT SHAPE]
{{"translations":[{{"id":"unchanged input id","text":"Korean translation"}}]}}
"""


def _validate_translated_text(
    *,
    block: SourceBlock,
    translated_text: str,
    termbase_entries: list[dict[str, Any]],
) -> list[str]:
    return [
        f"{block.id}: {issue.message}"
        for issue in check_translation_contract(
            source_text=block.effective_text,
            translated_text=translated_text,
            termbase_entries=termbase_entries,
        )
    ]


def parse_translation_response(
    *,
    response: Any,
    blocks: tuple[SourceBlock, ...],
    termbase_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Validate response structure and restore protected keep terms."""
    if not isinstance(response, dict) or not isinstance(
        response.get("translations"), list
    ):
        raise TranslationValidationError(
            "Translation response must contain a translations array."
        )
    expected_ids = [block.id for block in blocks]
    by_id = {block.id: block for block in blocks}
    translated: dict[str, str] = {}
    errors: list[str] = []
    for index, item in enumerate(response["translations"], start=1):
        if not isinstance(item, dict):
            errors.append(f"response item {index} is not an object")
            continue
        block_id = item.get("id")
        text = item.get("text")
        if not isinstance(block_id, str) or block_id not in expected_ids:
            errors.append(f"response item {index} has unknown id {block_id!r}")
            continue
        if block_id in translated:
            errors.append(f"response duplicates id {block_id}")
            continue
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{block_id}: translated text is empty")
            continue
        translated[block_id] = _restore_keep_terms(
            block=by_id[block_id],
            translated_text=text.strip(),
            entries=termbase_entries,
        )
    missing = [block_id for block_id in expected_ids if block_id not in translated]
    if missing:
        errors.append("missing ids: " + ", ".join(missing))
    if list(translated) != expected_ids:
        errors.append("response changed input id order")
    if len(response["translations"]) != len(expected_ids):
        errors.append(
            f"response returned {len(response['translations'])} items; "
            f"expected {len(expected_ids)}"
        )
    if errors:
        raise TranslationValidationError("; ".join(errors))
    return translated


def _translation_content_errors(
    *,
    translated: dict[str, str],
    blocks: tuple[SourceBlock, ...],
    termbase_entries: list[dict[str, Any]],
) -> list[str]:
    return [
        error
        for block in blocks
        for error in _validate_translated_text(
            block=block,
            translated_text=translated[block.id],
            termbase_entries=termbase_entries,
        )
    ]


def validate_translation_response(
    *,
    response: Any,
    blocks: tuple[SourceBlock, ...],
    termbase_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Strict validation used when a caller requires immediately clean output."""
    translated = parse_translation_response(
        response=response,
        blocks=blocks,
        termbase_entries=termbase_entries,
    )
    errors = _translation_content_errors(
        translated=translated,
        blocks=blocks,
        termbase_entries=termbase_entries,
    )
    if errors:
        raise TranslationValidationError("; ".join(errors))
    return translated


def _serialize_segments(segments: list[TranslationSegment]) -> bytes:
    return "".join(
        json.dumps(segment.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for segment in sorted(segments, key=lambda item: item.source_order)
    ).encode("utf-8")


def _parse_segments(data: bytes) -> list[TranslationSegment]:
    segments: list[TranslationSegment] = []
    line_number = 0
    try:
        for line_number, line in enumerate(
            data.decode("utf-8").splitlines(), start=1
        ):
            if line.strip():
                segments.append(TranslationSegment.from_dict(json.loads(line)))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TranslationSegmentValidationError,
        TypeError,
    ) as error:
        raise TranslationError(
            f"Invalid translation segment JSONL at line {line_number}: {error}"
        ) from error
    if len({segment.source_block_id for segment in segments}) != len(segments):
        raise TranslationError("Translation segment JSONL contains duplicate block IDs.")
    return segments


def _load_segments(path: Path) -> list[TranslationSegment]:
    if not path.is_file():
        return []
    try:
        return _parse_segments(path.read_bytes())
    except OSError as error:
        raise TranslationError(
            f"Could not read translation segment JSONL: {error}"
        ) from error


def _render_translation_review(segments: list[TranslationSegment]) -> bytes:
    lines = ["[[GLK_TRANSLATION_REVIEW version=1]]", ""]
    previous_marker: tuple[str, str | int | None] | None = None
    for segment in sorted(segments, key=lambda item: item.source_order):
        marker = (
            ("page", segment.page)
            if segment.page is not None
            else ("source", segment.source_file)
        )
        if marker != previous_marker:
            if previous_marker is not None:
                lines.extend(["", "======================", ""])
            if marker[0] == "page":
                lines.append(f"[PAGE {marker[1]}]")
            else:
                lines.append(f"[SOURCE {marker[1]}]")
            previous_marker = marker
        lines.extend(
            [
                f"[BLOCK {segment.source_block_id}]",
                "[ORIGINAL]",
                segment.source_text,
                "[TRANSLATION]",
                segment.translated_text,
                f"[[GLK_END {segment.source_block_id}]]",
                "",
            ]
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _translation_input_hash(
    *,
    approved_hash: str,
    termbase_hash: str,
    prompt_hash: str,
    model: str,
    provider_prompt_version: str,
    max_characters: int,
) -> str:
    value = {
        "version": TRANSLATION_RUN_VERSION,
        "approved_source_sha256": approved_hash,
        "termbase_sha256": termbase_hash,
        "project_prompt_sha256": prompt_hash,
        "hard_rules_version": TRANSLATION_HARD_RULES_VERSION,
        "model": model,
        "provider_prompt_version": provider_prompt_version,
        "max_characters": max_characters,
    }
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _prepare_translation_inputs(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    prompt_file: str | Path | None,
    model_name: str | None,
    max_characters: int,
    provider: TranslationProvider | None,
) -> _TranslationInputs:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if not pipeline["final_source_approved"]:
        raise TranslationError(
            "Final common source is not current. Complete glk review finalize first."
        )
    if pipeline["termbase_status"] != "current":
        raise TranslationError(
            "Termbase is not current. Complete glk glossary import first."
        )
    blocks, approved_data = _load_approved_blocks(location.path)
    termbase_entries, termbase_data = _load_termbase(location.path)
    project_instructions, prompt_path, prompt_needs_write = _resolve_prompt(
        prompt_file,
        location.path,
    )
    approved_hash = _sha256_bytes(approved_data)
    termbase_hash = _sha256_bytes(termbase_data)
    prompt_hash = _sha256_text(project_instructions)
    chunks = build_translation_chunks(
        blocks,
        max_characters=max_characters,
    )
    if provider is not None:
        active_model = provider.model_name
        provider_prompt_version = provider.prompt_version
    else:
        active_model = resolve_model_name(
            model_name,
            settings_root=settings_root,
        )
        provider_prompt_version = GeminiTranslationProvider.prompt_version
    input_hash = _translation_input_hash(
        approved_hash=approved_hash,
        termbase_hash=termbase_hash,
        prompt_hash=prompt_hash,
        model=active_model,
        provider_prompt_version=provider_prompt_version,
        max_characters=max_characters,
    )
    return _TranslationInputs(
        project_path=location.path,
        paths=paths,
        blocks=tuple(blocks),
        termbase_entries=tuple(termbase_entries),
        project_instructions=project_instructions,
        prompt_path=prompt_path,
        prompt_data=project_instructions.encode("utf-8"),
        prompt_needs_write=prompt_needs_write,
        approved_hash=approved_hash,
        termbase_hash=termbase_hash,
        prompt_hash=prompt_hash,
        chunks=tuple(chunks),
        active_model=active_model,
        provider_prompt_version=provider_prompt_version,
        input_hash=input_hash,
    )


def _dry_run_translation_result(
    inputs: _TranslationInputs,
) -> TranslationRunResult:
    prompt_path = inputs.prompt_path
    return TranslationRunResult(
        project_path=str(inputs.project_path),
        model=inputs.active_model,
        approved_source_sha256=inputs.approved_hash,
        termbase_sha256=inputs.termbase_hash,
        project_prompt_sha256=inputs.prompt_hash,
        input_sha256=inputs.input_hash,
        total_blocks=len(inputs.blocks),
        total_chunks=len(inputs.chunks),
        completed_blocks=0,
        completed_chunks=0,
        output_file=None,
        draft_file=None,
        review_file=None,
        review_status=None,
        prompt_file=(
            str(prompt_path)
            if prompt_path is not None and prompt_path.is_file()
            else None
        ),
        dry_run=True,
    )


def _ensure_translation_prompt(inputs: _TranslationInputs) -> Path:
    prompt_path = inputs.prompt_path
    if prompt_path is None:
        raise TranslationError(
            "Could not determine the project translation prompt path."
        )
    if inputs.prompt_needs_write or not prompt_path.is_file():
        _write_bytes_atomic(prompt_path, inputs.prompt_data)
    return prompt_path


def _cached_translation_result(
    inputs: _TranslationInputs,
    prompt_path: Path,
    previous_state: dict[str, Any],
    existing_segments: list[TranslationSegment],
) -> TranslationRunResult:
    paths = inputs.paths
    return TranslationRunResult(
        project_path=str(inputs.project_path),
        model=inputs.active_model,
        approved_source_sha256=inputs.approved_hash,
        termbase_sha256=inputs.termbase_hash,
        project_prompt_sha256=inputs.prompt_hash,
        input_sha256=inputs.input_hash,
        total_blocks=len(inputs.blocks),
        total_chunks=len(inputs.chunks),
        completed_blocks=len(existing_segments),
        completed_chunks=len(inputs.chunks),
        output_file=str(paths.translation_segments),
        draft_file=(
            str(paths.translation_draft)
            if paths.translation_draft.is_file()
            else None
        ),
        review_file=(
            str(paths.translation_review)
            if paths.translation_review.is_file()
            else None
        ),
        review_status=previous_state.get("review_status"),
        prompt_file=str(prompt_path),
        validation_issue_count=int(
            previous_state.get("validation_issue_count") or 0
        ),
        validation_issue_blocks=int(
            previous_state.get("validation_issue_blocks") or 0
        ),
        cached=True,
    )


def _restore_translation_checkpoint(
    inputs: _TranslationInputs,
    prompt_path: Path,
    *,
    resume: bool,
    force: bool,
) -> _TranslationCheckpoint | TranslationRunResult:
    output_path = inputs.paths.translation_segments
    previous_state = _read_json(inputs.paths.translation_state)
    existing_segments: list[TranslationSegment] = []
    existing_output_data = b""
    state_matches = bool(
        previous_state
        and previous_state.get("version") == TRANSLATION_RUN_VERSION
        and previous_state.get("input_sha256") == inputs.input_hash
    )
    empty_partial_checkpoint = bool(
        state_matches
        and previous_state
        and previous_state.get("status") == "partial"
        and previous_state.get("completed_blocks") == 0
        and previous_state.get("translation_output_sha256") is None
        and resume
    )
    if (
        previous_state is not None
        and state_matches
        and output_path.is_file()
        and not empty_partial_checkpoint
    ):
        existing_output_data = output_path.read_bytes()
        output_hash = _sha256_bytes(existing_output_data)
        expected_output_hash = previous_state.get("translation_output_sha256")
        if expected_output_hash != output_hash:
            checkpoint_bytes = previous_state.get("translation_output_bytes")
            can_restore_checkpoint = (
                previous_state.get("status") == "partial"
                and resume
                and isinstance(checkpoint_bytes, int)
                and not isinstance(checkpoint_bytes, bool)
                and 0 <= checkpoint_bytes <= len(existing_output_data)
            )
            checkpoint_data = (
                existing_output_data[:checkpoint_bytes]
                if can_restore_checkpoint
                else b""
            )
            if (
                not can_restore_checkpoint
                or _sha256_bytes(checkpoint_data) != expected_output_hash
            ):
                raise TranslationError(
                    "Translation output does not match its state. "
                    "Use --force after review."
                )
            _write_bytes_atomic(output_path, checkpoint_data)
            existing_output_data = checkpoint_data
        existing_segments = _parse_segments(existing_output_data)
        block_by_id = {block.id: block for block in inputs.blocks}
        if any(
            (block := block_by_id.get(segment.source_block_id)) is None
            or segment.source_text != block.effective_text
            or segment.source_sha256
            != _sha256_bytes(block.effective_text.encode("utf-8"))
            or segment.model != inputs.active_model
            or segment.prompt_sha256 != inputs.prompt_hash
            or segment.termbase_sha256 != inputs.termbase_hash
            for segment in existing_segments
        ):
            raise TranslationError(
                "Translation segments do not match current inputs. Use --force."
            )
        if (
            previous_state.get("status") == "complete"
            and len(existing_segments) == len(inputs.blocks)
            and not force
        ):
            return _cached_translation_result(
                inputs,
                prompt_path,
                previous_state,
                existing_segments,
            )
        if existing_segments and not resume and not force:
            raise TranslationError(
                "A partial translation exists. Use --resume to continue or --force "
                "to restart after review."
            )
    elif (
        previous_state
        and previous_state.get("status") == "partial"
        and previous_state.get("completed_blocks") == 0
        and resume
    ):
        existing_segments = []
    elif (previous_state or output_path.is_file()) and not force:
        raise TranslationError(
            "Existing translation inputs are stale or incomplete. Compare existing "
            "outputs, then use --force to restart."
        )
    if force:
        existing_segments = []
        existing_output_data = b""
    return _TranslationCheckpoint(
        previous_state=previous_state,
        existing_segments=tuple(existing_segments),
        existing_output_data=existing_output_data,
    )


def _request_translation_chunk(
    chunk: TranslationChunk,
    *,
    chunk_index: int,
    total_chunks: int,
    provider: TranslationProvider,
    termbase_entries: list[dict[str, Any]],
    project_instructions: str,
    notify: ProgressCallback,
) -> dict[str, str]:
    feedback: str | None = None
    structurally_valid: dict[str, str] | None = None
    for validation_attempt in range(2):
        prompt = compile_translation_prompt(
            blocks=chunk.blocks,
            termbase_entries=termbase_entries,
            project_instructions=project_instructions,
            validation_feedback=feedback,
        )
        try:
            response = provider.translate(prompt)
            candidate = parse_translation_response(
                response=response,
                blocks=chunk.blocks,
                termbase_entries=termbase_entries,
            )
        except TranslationValidationError as error:
            feedback = str(error)
            notify(
                f"Chunk {chunk_index}/{total_chunks}: "
                f"response structure validation failed "
                f"({validation_attempt + 1}/2)"
            )
            continue
        except Exception:
            if structurally_valid is not None:
                notify(
                    f"Chunk {chunk_index}/{total_chunks}: "
                    "content validation retry failed; "
                    "preserving the reviewable response"
                )
                break
            raise
        structurally_valid = candidate
        content_errors = _translation_content_errors(
            translated=candidate,
            blocks=chunk.blocks,
            termbase_entries=termbase_entries,
        )
        if not content_errors:
            return candidate
        feedback = "; ".join(content_errors)
        notify(
            f"Chunk {chunk_index}/{total_chunks}: "
            f"content validation needs review "
            f"({validation_attempt + 1}/2)"
        )
    if structurally_valid is not None:
        notify(
            f"Chunk {chunk_index}/{total_chunks}: "
            "saved with content issues for human review"
        )
        return structurally_valid
    raise TranslationValidationError(
        feedback or f"Chunk {chunk.id} failed response validation."
    )


def _build_translation_segments(
    chunk: TranslationChunk,
    translated: dict[str, str],
    *,
    provider: TranslationProvider,
    termbase_entries: list[dict[str, Any]],
    prompt_hash: str,
    termbase_hash: str,
) -> tuple[list[TranslationSegment], list[str], set[str]]:
    chunk_segments: list[TranslationSegment] = []
    issue_messages: list[str] = []
    issue_block_ids: set[str] = set()
    for block in chunk.blocks:
        translated_text = translated[block.id]
        content_errors = _validate_translated_text(
            block=block,
            translated_text=translated_text,
            termbase_entries=termbase_entries,
        )
        if content_errors:
            issue_messages.extend(content_errors)
            issue_block_ids.add(block.id)
        segment = TranslationSegment(
            schema_version=TRANSLATION_SEGMENT_SCHEMA_VERSION,
            source_block_id=block.id,
            source_file=block.source_file,
            page=block.page,
            source_order=block.source_order,
            block_type=block.block_type,
            source_text=block.effective_text,
            source_sha256=_sha256_bytes(
                block.effective_text.encode("utf-8")
            ),
            translated_text=translated_text,
            translation_sha256=_sha256_bytes(
                translated_text.encode("utf-8")
            ),
            status="flagged" if content_errors else "translated",
            model=provider.model_name,
            prompt_sha256=prompt_hash,
            termbase_sha256=termbase_hash,
        )
        segment.validate()
        chunk_segments.append(segment)
    return chunk_segments, issue_messages, issue_block_ids


def _write_partial_translation_state(
    inputs: _TranslationInputs,
    provider: TranslationProvider,
    *,
    max_characters: int,
    completed_blocks: int,
    completed_chunks: int,
    output_hash: str | None,
    output_bytes: int,
    failed_chunk: str | None,
    failure_reason: str | None,
    validation_issue_count: int,
    validation_issue_blocks: int,
) -> None:
    _write_json_atomic(
        inputs.paths.translation_state,
        {
            "schema_version": 1,
            "status": "partial",
            "version": TRANSLATION_RUN_VERSION,
            "input_sha256": inputs.input_hash,
            "approved_source_sha256": inputs.approved_hash,
            "termbase_sha256": inputs.termbase_hash,
            "project_prompt_sha256": inputs.prompt_hash,
            "project_prompt_file": inputs.paths.relative(
                inputs.paths.translation_prompt
            ),
            "model": provider.model_name,
            "provider_prompt_version": provider.prompt_version,
            "hard_rules_version": TRANSLATION_HARD_RULES_VERSION,
            "max_characters": max_characters,
            "total_blocks": len(inputs.blocks),
            "total_chunks": len(inputs.chunks),
            "completed_blocks": completed_blocks,
            "completed_chunks": completed_chunks,
            "translation_output_sha256": output_hash,
            "translation_output_bytes": output_bytes,
            "failed_chunk": failed_chunk,
            "failure_reason": failure_reason,
            "validation_issue_count": validation_issue_count,
            "validation_issue_blocks": validation_issue_blocks,
            "updated_at": _utc_now(),
        },
    )


def _prepare_translation_execution(
    inputs: _TranslationInputs,
    provider: TranslationProvider,
    checkpoint: _TranslationCheckpoint,
    *,
    max_characters: int,
) -> _TranslationExecution:
    existing_segments = list(checkpoint.existing_segments)
    completed = {
        segment.source_block_id: segment for segment in existing_segments
    }
    execution = _TranslationExecution(
        completed=completed,
        output_digest=hashlib.sha256(checkpoint.existing_output_data),
        output_bytes=len(checkpoint.existing_output_data),
        completed_chunks=0,
        validation_issue_messages=[],
        validation_issue_block_ids=set(),
    )
    block_by_id = {block.id: block for block in inputs.blocks}
    for segment in existing_segments:
        block = block_by_id[segment.source_block_id]
        errors = _validate_translated_text(
            block=block,
            translated_text=segment.translated_text,
            termbase_entries=list(inputs.termbase_entries),
        )
        if errors:
            execution.validation_issue_messages.extend(errors)
            execution.validation_issue_block_ids.add(block.id)
    if not existing_segments:
        _write_partial_translation_state(
            inputs,
            provider,
            max_characters=max_characters,
            completed_blocks=0,
            completed_chunks=0,
            output_hash=None,
            output_bytes=0,
            failed_chunk=None,
            failure_reason=None,
            validation_issue_count=0,
            validation_issue_blocks=0,
        )
    return execution


def _translate_pending_chunks(
    inputs: _TranslationInputs,
    provider: TranslationProvider,
    execution: _TranslationExecution,
    *,
    max_characters: int,
    notify: ProgressCallback,
) -> None:
    chunks = list(inputs.chunks)
    termbase_entries = list(inputs.termbase_entries)
    for chunk_index, chunk in enumerate(chunks, start=1):
        if all(block.id in execution.completed for block in chunk.blocks):
            execution.completed_chunks += 1
            notify(
                f"Chunk {chunk_index}/{len(chunks)}: reused completed translation"
            )
            continue
        if any(block.id in execution.completed for block in chunk.blocks):
            raise TranslationError(
                f"Chunk {chunk.id} is only partially stored. Use --force to restart."
            )
        notify(f"Chunk {chunk_index}/{len(chunks)}: requesting translation")
        try:
            translated = _request_translation_chunk(
                chunk,
                chunk_index=chunk_index,
                total_chunks=len(chunks),
                provider=provider,
                termbase_entries=termbase_entries,
                project_instructions=inputs.project_instructions,
                notify=notify,
            )
        except Exception as error:
            output_hash = (
                execution.output_digest.hexdigest()
                if execution.output_bytes > 0
                else None
            )
            _write_partial_translation_state(
                inputs,
                provider,
                max_characters=max_characters,
                completed_blocks=len(execution.completed),
                completed_chunks=execution.completed_chunks,
                output_hash=output_hash,
                output_bytes=execution.output_bytes,
                failed_chunk=chunk.id,
                failure_reason=str(error),
                validation_issue_count=len(
                    execution.validation_issue_messages
                ),
                validation_issue_blocks=len(
                    execution.validation_issue_block_ids
                ),
            )
            raise TranslationError(
                f"Translation failed for {chunk.id}. Completed chunks were preserved; "
                f"fix the issue and use --resume. Cause: {error}"
            ) from error

        chunk_segments, issue_messages, issue_block_ids = (
            _build_translation_segments(
                chunk,
                translated,
                provider=provider,
                termbase_entries=termbase_entries,
                prompt_hash=inputs.prompt_hash,
                termbase_hash=inputs.termbase_hash,
            )
        )
        execution.validation_issue_messages.extend(issue_messages)
        execution.validation_issue_block_ids.update(issue_block_ids)
        for segment in chunk_segments:
            execution.completed[segment.source_block_id] = segment
        execution.completed_chunks += 1
        chunk_data = _serialize_segments(chunk_segments)
        if execution.output_bytes:
            _append_bytes_durable(inputs.paths.translation_segments, chunk_data)
        else:
            _write_bytes_atomic(inputs.paths.translation_segments, chunk_data)
        execution.output_digest.update(chunk_data)
        execution.output_bytes += len(chunk_data)
        _write_partial_translation_state(
            inputs,
            provider,
            max_characters=max_characters,
            completed_blocks=len(execution.completed),
            completed_chunks=execution.completed_chunks,
            output_hash=execution.output_digest.hexdigest(),
            output_bytes=execution.output_bytes,
            failed_chunk=None,
            failure_reason=None,
            validation_issue_count=len(execution.validation_issue_messages),
            validation_issue_blocks=len(
                execution.validation_issue_block_ids
            ),
        )


def _finalize_translation_run(
    inputs: _TranslationInputs,
    prompt_path: Path,
    provider: TranslationProvider,
    *,
    max_characters: int,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    completed: dict[str, TranslationSegment],
    output_digest: Any,
    output_bytes: int,
    validation_issue_messages: list[str],
    validation_issue_block_ids: set[str],
) -> TranslationRunResult:
    paths = inputs.paths
    ordered_segments = sorted(
        completed.values(),
        key=lambda item: item.source_order,
    )
    if len(ordered_segments) != len(inputs.blocks):
        raise TranslationError(
            "Translation completed without every approved source block."
        )
    output_data = _serialize_segments(ordered_segments)
    if output_bytes == 0:
        _write_bytes_atomic(paths.translation_segments, output_data)
        output_digest = hashlib.sha256(output_data)
        output_bytes = len(output_data)
    output_hash = output_digest.hexdigest()
    if (
        output_bytes != len(output_data)
        or output_hash != _sha256_bytes(output_data)
    ):
        raise TranslationError(
            "Translation checkpoint does not match completed segments."
        )
    review_data = _render_translation_review(ordered_segments)
    draft_hash = _sha256_bytes(review_data)
    _write_bytes_atomic(paths.translation_draft, review_data)
    review_created = not paths.translation_review.is_file()
    review_base_draft_hash: str | None
    if review_created:
        _write_bytes_atomic(paths.translation_review, review_data)
        review_status = "current"
        review_base_draft_hash = draft_hash
    else:
        previous_base = (
            previous_state.get("review_base_draft_sha256")
            if previous_state
            else None
        )
        review_status = "current" if previous_base == draft_hash else "stale"
        review_base_draft_hash = (
            previous_base if isinstance(previous_base, str) else None
        )
    _write_json_atomic(
        paths.translation_state,
        {
            "schema_version": 1,
            "status": "complete",
            "version": TRANSLATION_RUN_VERSION,
            "input_sha256": inputs.input_hash,
            "approved_source_sha256": inputs.approved_hash,
            "termbase_sha256": inputs.termbase_hash,
            "project_prompt_sha256": inputs.prompt_hash,
            "project_prompt_file": paths.relative(paths.translation_prompt),
            "hard_rules_version": TRANSLATION_HARD_RULES_VERSION,
            "model": provider.model_name,
            "provider_prompt_version": provider.prompt_version,
            "max_characters": max_characters,
            "total_blocks": len(inputs.blocks),
            "total_chunks": len(inputs.chunks),
            "completed_blocks": len(ordered_segments),
            "completed_chunks": len(inputs.chunks),
            "translation_output_file": paths.relative(
                paths.translation_segments
            ),
            "translation_output_sha256": output_hash,
            "translation_output_bytes": output_bytes,
            "draft_file": paths.relative(paths.translation_draft),
            "draft_sha256": draft_hash,
            "review_file": paths.relative(paths.translation_review),
            "review_status": review_status,
            "review_base_draft_sha256": review_base_draft_hash,
            "failed_chunk": None,
            "failure_reason": None,
            "validation_issue_count": len(validation_issue_messages),
            "validation_issue_blocks": len(validation_issue_block_ids),
            "updated_at": _utc_now(),
        },
    )
    return TranslationRunResult(
        project_path=str(inputs.project_path),
        model=provider.model_name,
        approved_source_sha256=inputs.approved_hash,
        termbase_sha256=inputs.termbase_hash,
        project_prompt_sha256=inputs.prompt_hash,
        input_sha256=inputs.input_hash,
        total_blocks=len(inputs.blocks),
        total_chunks=len(inputs.chunks),
        completed_blocks=len(ordered_segments),
        completed_chunks=len(inputs.chunks),
        output_file=str(paths.translation_segments),
        draft_file=str(paths.translation_draft),
        review_file=str(paths.translation_review),
        review_status=review_status,
        prompt_file=str(prompt_path),
        validation_issue_count=len(validation_issue_messages),
        validation_issue_blocks=len(validation_issue_block_ids),
        resumed=resumed,
        review_created=review_created,
    )


def translate_project(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    settings_root: str | Path | None = None,
    prompt_file: str | Path | None = None,
    model_name: str | None = None,
    max_characters: int = 10000,
    resume: bool = False,
    force: bool = False,
    dry_run: bool = False,
    provider: TranslationProvider | None = None,
    progress: ProgressCallback | None = None,
) -> TranslationRunResult:
    notify = progress or (lambda _: None)
    inputs = _prepare_translation_inputs(
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        prompt_file=prompt_file,
        model_name=model_name,
        max_characters=max_characters,
        provider=provider,
    )
    if dry_run:
        return _dry_run_translation_result(inputs)
    canonical_prompt_path = _ensure_translation_prompt(inputs)
    restored = _restore_translation_checkpoint(
        inputs,
        canonical_prompt_path,
        resume=resume,
        force=force,
    )
    if isinstance(restored, TranslationRunResult):
        return restored
    active_provider = provider or GeminiTranslationProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )
    execution = _prepare_translation_execution(
        inputs,
        active_provider,
        restored,
        max_characters=max_characters,
    )
    _translate_pending_chunks(
        inputs,
        active_provider,
        execution,
        max_characters=max_characters,
        notify=notify,
    )

    return _finalize_translation_run(
        inputs,
        canonical_prompt_path,
        active_provider,
        max_characters=max_characters,
        previous_state=restored.previous_state,
        resumed=bool(restored.existing_segments),
        completed=execution.completed,
        output_digest=execution.output_digest,
        output_bytes=execution.output_bytes,
        validation_issue_messages=execution.validation_issue_messages,
        validation_issue_block_ids=execution.validation_issue_block_ids,
    )
