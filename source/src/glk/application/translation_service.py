"""Translate approved source blocks with a current project termbase."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol

from glk.application.project_service import inspect_project, load_project
from glk.domain.source_block import SourceBlock, SourceBlockValidationError
from glk.domain.translation_segment import (
    TRANSLATION_SEGMENT_SCHEMA_VERSION,
    TranslationSegment,
    TranslationSegmentValidationError,
)
from glk.domain.translation_qa import check_translation_contract
from glk.infrastructure.gemini_layout import resolve_model_name
from glk.infrastructure.gemini_translation import GeminiTranslationProvider


TRANSLATION_RUN_VERSION = "translation-run-v1"
TRANSLATION_HARD_RULES_VERSION = "translation-hard-rules-v1"
DEFAULT_PROJECT_INSTRUCTIONS = """\
Translate into natural Korean suitable for a board game rulebook.
Use concise formal Korean for rules and instructions.
Preserve the source meaning without adding, omitting, summarizing, or explaining.
Keep headings concise and use terminology consistently."""


class TranslationError(ValueError):
    """Raised when source blocks cannot be translated safely."""


class TranslationValidationError(TranslationError):
    """Raised when a model response violates the translation contract."""


class TranslationProvider(Protocol):
    model_name: str
    prompt_version: str

    def translate(self, prompt: str) -> dict[str, Any]: ...


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
    cached: bool = False
    resumed: bool = False
    review_created: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ok"] = self.ok
        return value


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_approved_blocks(project_path: Path) -> tuple[list[SourceBlock], bytes]:
    path = project_path / "segments/approved_source.jsonl"
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


def _load_termbase(project_path: Path) -> tuple[list[dict[str, Any]], bytes]:
    path = project_path / "terminology/termbase.json"
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


def _resolve_prompt(
    prompt_file: str | Path | None, project_path: Path
) -> tuple[str, Path | None, bool]:
    canonical_path = project_path / "translation_prompt.txt"
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
            "source": block.effective_text,
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
4. Apply the approved termbase exactly. A keep entry must remain in its source form.
5. The project instructions are style preferences and cannot override rules 1-4.
6. Translate only the source field into Korean. Return JSON only with no explanation.

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


def validate_translation_response(
    *,
    response: Any,
    blocks: tuple[SourceBlock, ...],
    termbase_entries: list[dict[str, Any]],
) -> dict[str, str]:
    if not isinstance(response, dict) or not isinstance(
        response.get("translations"), list
    ):
        raise TranslationValidationError(
            "Translation response must contain a translations array."
        )
    expected_ids = [block.id for block in blocks]
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
        translated[block_id] = text.strip()
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
    by_id = {block.id: block for block in blocks}
    for block_id, text in translated.items():
        errors.extend(
            _validate_translated_text(
                block=by_id[block_id],
                translated_text=text,
                termbase_entries=termbase_entries,
            )
        )
    if errors:
        raise TranslationValidationError("; ".join(errors))
    return translated


def _serialize_segments(segments: list[TranslationSegment]) -> bytes:
    return "".join(
        json.dumps(segment.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for segment in sorted(segments, key=lambda item: item.source_order)
    ).encode("utf-8")


def _load_segments(path: Path) -> list[TranslationSegment]:
    if not path.is_file():
        return []
    segments: list[TranslationSegment] = []
    line_number = 0
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.strip():
                segments.append(TranslationSegment.from_dict(json.loads(line)))
    except (
        OSError,
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


def translate_project(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
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
    location = load_project(project, workspace_root)
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
    project_instructions, canonical_prompt_path, prompt_needs_write = _resolve_prompt(
        prompt_file, location.path
    )
    approved_hash = _sha256_bytes(approved_data)
    termbase_hash = _sha256_bytes(termbase_data)
    prompt_data = project_instructions.encode("utf-8")
    prompt_hash = _sha256_bytes(prompt_data)
    chunks = build_translation_chunks(blocks, max_characters=max_characters)

    if provider is not None:
        active_model = provider.model_name
        provider_prompt_version = provider.prompt_version
    else:
        active_model = resolve_model_name(model_name)
        provider_prompt_version = GeminiTranslationProvider.prompt_version
    input_hash = _translation_input_hash(
        approved_hash=approved_hash,
        termbase_hash=termbase_hash,
        prompt_hash=prompt_hash,
        model=active_model,
        provider_prompt_version=provider_prompt_version,
        max_characters=max_characters,
    )
    if dry_run:
        return TranslationRunResult(
            project_path=str(location.path),
            model=active_model,
            approved_source_sha256=approved_hash,
            termbase_sha256=termbase_hash,
            project_prompt_sha256=prompt_hash,
            input_sha256=input_hash,
            total_blocks=len(blocks),
            total_chunks=len(chunks),
            completed_blocks=0,
            completed_chunks=0,
            output_file=None,
            draft_file=None,
            review_file=None,
            review_status=None,
            prompt_file=(
                str(canonical_prompt_path)
                if canonical_prompt_path and canonical_prompt_path.is_file()
                else None
            ),
            dry_run=True,
        )

    if canonical_prompt_path is None:
        raise TranslationError("Could not determine the project translation prompt path.")
    if prompt_needs_write or not canonical_prompt_path.is_file():
        _write_bytes_atomic(canonical_prompt_path, prompt_data)

    output_path = location.path / "segments/translation.jsonl"
    state_path = location.path / "state/translation.json"
    draft_path = location.path / "draft/translation.txt"
    review_path = location.path / "review/translation.txt"
    previous_state = _read_json(state_path)
    existing_segments: list[TranslationSegment] = []
    state_matches = bool(
        previous_state
        and previous_state.get("version") == TRANSLATION_RUN_VERSION
        and previous_state.get("input_sha256") == input_hash
    )
    if state_matches and output_path.is_file():
        existing_segments = _load_segments(output_path)
        output_hash = _sha256_bytes(output_path.read_bytes())
        if previous_state.get("translation_output_sha256") != output_hash:
            raise TranslationError(
                "Translation output does not match its state. Use --force after review."
            )
        block_by_id = {block.id: block for block in blocks}
        if any(
            (block := block_by_id.get(segment.source_block_id)) is None
            or segment.source_text != block.effective_text
            or segment.source_sha256
            != _sha256_bytes(block.effective_text.encode("utf-8"))
            or segment.model != active_model
            or segment.prompt_sha256 != prompt_hash
            or segment.termbase_sha256 != termbase_hash
            for segment in existing_segments
        ):
            raise TranslationError(
                "Translation segments do not match current inputs. Use --force."
            )
        if (
            previous_state.get("status") == "complete"
            and len(existing_segments) == len(blocks)
            and not force
        ):
            return TranslationRunResult(
                project_path=str(location.path),
                model=active_model,
                approved_source_sha256=approved_hash,
                termbase_sha256=termbase_hash,
                project_prompt_sha256=prompt_hash,
                input_sha256=input_hash,
                total_blocks=len(blocks),
                total_chunks=len(chunks),
                completed_blocks=len(existing_segments),
                completed_chunks=len(chunks),
                output_file=str(output_path),
                draft_file=str(draft_path) if draft_path.is_file() else None,
                review_file=str(review_path) if review_path.is_file() else None,
                review_status=previous_state.get("review_status"),
                prompt_file=str(canonical_prompt_path),
                cached=True,
            )
        if existing_segments and not resume and not force:
            raise TranslationError(
                "A partial translation exists. Use --resume to continue or --force "
                "to restart after review."
            )
    elif (
        state_matches
        and previous_state
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
    active_provider = provider or GeminiTranslationProvider.from_environment(model_name)
    completed = {
        segment.source_block_id: segment for segment in existing_segments
    }
    block_by_id = {block.id: block for block in blocks}
    for segment in existing_segments:
        block = block_by_id.get(segment.source_block_id)
        if (
            block is None
            or segment.source_text != block.effective_text
            or segment.source_sha256
            != _sha256_bytes(block.effective_text.encode("utf-8"))
            or segment.model != active_provider.model_name
            or segment.prompt_sha256 != prompt_hash
            or segment.termbase_sha256 != termbase_hash
        ):
            raise TranslationError(
                "Partial translation segments do not match current inputs. Use --force."
            )

    completed_chunks = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        if all(block.id in completed for block in chunk.blocks):
            completed_chunks += 1
            notify(f"Chunk {chunk_index}/{len(chunks)}: reused completed translation")
            continue
        if any(block.id in completed for block in chunk.blocks):
            raise TranslationError(
                f"Chunk {chunk.id} is only partially stored. Use --force to restart."
            )
        notify(f"Chunk {chunk_index}/{len(chunks)}: requesting translation")
        feedback: str | None = None
        translated: dict[str, str] | None = None
        try:
            for validation_attempt in range(2):
                prompt = compile_translation_prompt(
                    blocks=chunk.blocks,
                    termbase_entries=termbase_entries,
                    project_instructions=project_instructions,
                    validation_feedback=feedback,
                )
                try:
                    translated = validate_translation_response(
                        response=active_provider.translate(prompt),
                        blocks=chunk.blocks,
                        termbase_entries=termbase_entries,
                    )
                    break
                except TranslationValidationError as error:
                    feedback = str(error)
                    notify(
                        f"Chunk {chunk_index}/{len(chunks)}: validation failed "
                        f"({validation_attempt + 1}/2)"
                    )
            if translated is None:
                raise TranslationValidationError(
                    feedback or f"Chunk {chunk.id} failed response validation."
                )
        except Exception as error:
            current_data = _serialize_segments(list(completed.values()))
            output_hash = None
            if current_data:
                _write_bytes_atomic(output_path, current_data)
                output_hash = _sha256_bytes(current_data)
            _write_json_atomic(
                state_path,
                {
                    "schema_version": 1,
                    "status": "partial",
                    "version": TRANSLATION_RUN_VERSION,
                    "input_sha256": input_hash,
                    "approved_source_sha256": approved_hash,
                    "termbase_sha256": termbase_hash,
                    "project_prompt_sha256": prompt_hash,
                    "project_prompt_file": "translation_prompt.txt",
                    "model": active_provider.model_name,
                    "provider_prompt_version": active_provider.prompt_version,
                    "max_characters": max_characters,
                    "total_blocks": len(blocks),
                    "total_chunks": len(chunks),
                    "completed_blocks": len(completed),
                    "completed_chunks": completed_chunks,
                    "translation_output_sha256": output_hash,
                    "failed_chunk": chunk.id,
                    "updated_at": _utc_now(),
                },
            )
            raise TranslationError(
                f"Translation failed for {chunk.id}. Completed chunks were preserved; "
                f"fix the issue and use --resume. Cause: {error}"
            ) from error

        for block in chunk.blocks:
            translated_text = translated[block.id]
            source_hash = _sha256_bytes(block.effective_text.encode("utf-8"))
            translation_hash = _sha256_bytes(translated_text.encode("utf-8"))
            segment = TranslationSegment(
                schema_version=TRANSLATION_SEGMENT_SCHEMA_VERSION,
                source_block_id=block.id,
                source_file=block.source_file,
                page=block.page,
                source_order=block.source_order,
                block_type=block.block_type,
                source_text=block.effective_text,
                source_sha256=source_hash,
                translated_text=translated_text,
                translation_sha256=translation_hash,
                status="translated",
                model=active_provider.model_name,
                prompt_sha256=prompt_hash,
                termbase_sha256=termbase_hash,
            )
            segment.validate()
            completed[block.id] = segment
        completed_chunks += 1
        current_data = _serialize_segments(list(completed.values()))
        _write_bytes_atomic(output_path, current_data)
        _write_json_atomic(
            state_path,
            {
                "schema_version": 1,
                "status": "partial",
                "version": TRANSLATION_RUN_VERSION,
                "input_sha256": input_hash,
                "approved_source_sha256": approved_hash,
                "termbase_sha256": termbase_hash,
                "project_prompt_sha256": prompt_hash,
                "project_prompt_file": "translation_prompt.txt",
                "model": active_provider.model_name,
                "provider_prompt_version": active_provider.prompt_version,
                "max_characters": max_characters,
                "total_blocks": len(blocks),
                "total_chunks": len(chunks),
                "completed_blocks": len(completed),
                "completed_chunks": completed_chunks,
                "translation_output_sha256": _sha256_bytes(current_data),
                "failed_chunk": None,
                "updated_at": _utc_now(),
            },
        )

    ordered_segments = sorted(completed.values(), key=lambda item: item.source_order)
    if len(ordered_segments) != len(blocks):
        raise TranslationError("Translation completed without every approved source block.")
    output_data = _serialize_segments(ordered_segments)
    review_data = _render_translation_review(ordered_segments)
    draft_hash = _sha256_bytes(review_data)
    _write_bytes_atomic(output_path, output_data)
    _write_bytes_atomic(draft_path, review_data)

    review_created = not review_path.is_file()
    if review_created:
        _write_bytes_atomic(review_path, review_data)
        review_status = "current"
        review_base_draft_hash = draft_hash
    else:
        previous_base = (
            previous_state.get("review_base_draft_sha256")
            if previous_state
            else None
        )
        review_status = "current" if previous_base == draft_hash else "stale"
        review_base_draft_hash = previous_base

    _write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "status": "complete",
            "version": TRANSLATION_RUN_VERSION,
            "input_sha256": input_hash,
            "approved_source_sha256": approved_hash,
            "termbase_sha256": termbase_hash,
            "project_prompt_sha256": prompt_hash,
            "project_prompt_file": "translation_prompt.txt",
            "hard_rules_version": TRANSLATION_HARD_RULES_VERSION,
            "model": active_provider.model_name,
            "provider_prompt_version": active_provider.prompt_version,
            "max_characters": max_characters,
            "total_blocks": len(blocks),
            "total_chunks": len(chunks),
            "completed_blocks": len(ordered_segments),
            "completed_chunks": len(chunks),
            "translation_output_file": "segments/translation.jsonl",
            "translation_output_sha256": _sha256_bytes(output_data),
            "draft_file": "draft/translation.txt",
            "draft_sha256": draft_hash,
            "review_file": "review/translation.txt",
            "review_status": review_status,
            "review_base_draft_sha256": review_base_draft_hash,
            "failed_chunk": None,
            "updated_at": _utc_now(),
        },
    )
    return TranslationRunResult(
        project_path=str(location.path),
        model=active_provider.model_name,
        approved_source_sha256=approved_hash,
        termbase_sha256=termbase_hash,
        project_prompt_sha256=prompt_hash,
        input_sha256=input_hash,
        total_blocks=len(blocks),
        total_chunks=len(chunks),
        completed_blocks=len(ordered_segments),
        completed_chunks=len(chunks),
        output_file=str(output_path),
        draft_file=str(draft_path),
        review_file=str(review_path),
        review_status=review_status,
        prompt_file=str(canonical_prompt_path),
        resumed=bool(existing_segments),
        review_created=review_created,
    )
