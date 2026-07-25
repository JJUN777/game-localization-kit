"""Build and import project terminology from approved source blocks."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any

from glk.application._cache import read_json_object
from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application._io import write_json_atomic as _write_json_atomic
from glk.application.project_service import inspect_project, load_project
from glk.domain.source_block import SourceBlock, SourceBlockValidationError
from glk.domain.workspace import IMAGE_SOURCE_ROOT, WorkspacePaths


GLOSSARY_BUILD_VERSION = "glossary-candidates-local-v2"
GLOSSARY_IMPORT_VERSION = "termbase-import-v1"
GLOSSARY_REVIEW_COLUMNS = (
    "status",
    "source_term",
    "translation",
    "category",
    "note",
    "variants",
    "occurrences",
    "locations",
    "example",
    "candidate_id",
)
_TOKEN_PATTERN = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")
_QUANTITY_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+\s*[x×](?=\s|$)", re.IGNORECASE)
_LIST_ENUMERATOR_PATTERN = re.compile(r"^\s*\d+[A-Za-z]?\.\s*")
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")
_SEGMENT_SPLIT_PATTERN = re.compile(r"[\n.!?;:,()\[\]{}]+")
_LOWER_CONNECTORS = {"of", "the", "and", "for", "to", "in", "on", "from", "with", "&"}
_STOPWORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "before",
    "by",
    "can",
    "during",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "must",
    "no",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "up",
    "was",
    "were",
    "when",
    "while",
    "with",
    "without",
    "you",
    "your",
    "chapter",
    "contents",
    "example",
    "examples",
    "figure",
    "display",
    "feature",
    "following",
    "game",
    "games",
    "name",
    "note",
    "only",
    "overview",
    "page",
    "represent",
    "rulebook",
    "side",
    "setup",
    "table",
    "unique",
    "used",
    "using",
    "version",
    "will",
}
_HEADING_TYPES = {"title", "heading", "ability", "identifier", "label"}
_COMPONENT_WORDS = {
    "board",
    "card",
    "cards",
    "counter",
    "deck",
    "die",
    "dice",
    "icon",
    "marker",
    "miniature",
    "pile",
    "slot",
    "slots",
    "tile",
    "token",
    "track",
}
_GLOSSARY_STATUSES = {"review", "approved", "keep", "rejected"}
_GLOSSARY_CATEGORIES = {
    "term",
    "proper_noun",
    "ability",
    "component",
    "ui",
    "phrase",
}


class GlossaryBuildError(ValueError):
    """Raised when glossary candidates cannot be built safely."""


class GlossaryReviewStaleError(GlossaryBuildError):
    """Raised when reviewed candidates must not be overwritten automatically."""

    code = "GLOSSARY_REVIEW_STALE"


class GlossaryImportError(ValueError):
    """Raised when a reviewed glossary cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class GlossaryCandidate:
    candidate_id: str
    source_term: str
    category: str
    variants: tuple[str, ...]
    occurrences: int
    locations: tuple[str, ...]
    example: str
    score: float

    def to_review_row(self) -> dict[str, str]:
        return {
            "status": "review",
            "source_term": self.source_term,
            "translation": "",
            "category": self.category,
            "note": "",
            "variants": " | ".join(self.variants),
            "occurrences": str(self.occurrences),
            "locations": ",".join(self.locations),
            "example": self.example,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True, slots=True)
class GlossaryBuildResult:
    project_path: str
    approved_source_sha256: str
    candidate_count: int
    output_file: str | None
    status: str
    created: bool = False
    reset: bool = False
    cached: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GlossaryImportResult:
    project_path: str
    approved_source_sha256: str
    review_tsv_sha256: str
    entry_count: int
    active_count: int
    rejected_count: int
    manual_count: int
    unverified_count: int
    review_file: str
    output_file: str
    status: str = "current"
    cached: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _GlossaryImportContext:
    project_path: Path
    project_id: str
    source_language: str
    target_language: str
    paths: WorkspacePaths
    blocks: tuple[SourceBlock, ...]
    approved_hash: str
    expected_candidate_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _GlossaryImportPayload:
    normalized_tsv: bytes
    review_hash: str
    entries: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    @property
    def active_count(self) -> int:
        return sum(
            item["status"] in {"approved", "keep"}
            for item in self.entries
        )

    @property
    def rejected_count(self) -> int:
        return sum(item["status"] == "rejected" for item in self.entries)

    @property
    def manual_count(self) -> int:
        return sum(item["origin"] == "manual" for item in self.entries)

    @property
    def unverified_count(self) -> int:
        return sum(not item["source_verified"] for item in self.entries)


@dataclass(slots=True)
class _GlossaryImportTracker:
    seen_terms: dict[str, int]
    seen_term_keys: dict[str, int]
    seen_ids: dict[str, int]
    seen_automatic_ids: set[str]


@dataclass(frozen=True, slots=True)
class _GlossaryRowFields:
    status: str
    source_term: str
    translation: str
    category: str
    note: str
    candidate_id: str


@dataclass(slots=True)
class _Occurrence:
    surface: str
    block_id: str
    source_order: int
    location: str
    example: str
    block_type: str
    segment_index: int
    start: int
    end: int
    heading_evidence: bool
    title_evidence: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_approved_blocks(project_path: Path) -> tuple[list[SourceBlock], bytes]:
    path = WorkspacePaths(project_path).approved_source_segments
    if not path.is_file():
        raise GlossaryBuildError(
            f"Final common source not found: {path}. Run glk review finalize first."
        )
    data = path.read_bytes()
    blocks: list[SourceBlock] = []
    line_number = 0
    try:
        for line_number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
            if line.strip():
                block = SourceBlock.from_dict(json.loads(line))
                if block.status != "approved":
                    raise GlossaryBuildError(
                        f"Approved source contains non-approved block {block.id}."
                    )
                blocks.append(block)
    except GlossaryBuildError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceBlockValidationError,
        TypeError,
    ) as error:
        raise GlossaryBuildError(
            f"Invalid approved source JSONL at line {line_number}: {error}"
        ) from error
    if not blocks:
        raise GlossaryBuildError("Final common source is empty.")
    if len({block.id for block in blocks}) != len(blocks):
        raise GlossaryBuildError("Final common source contains duplicate block IDs.")
    return blocks, data


def _singularize_word(word: str) -> str:
    lower = word.casefold()
    if len(lower) > 4 and lower.endswith("ies"):
        return lower[:-3] + "y"
    if len(lower) > 4 and lower.endswith(("ches", "shes", "xes", "zes")):
        return lower[:-2]
    if (
        len(lower) > 3
        and lower.endswith("s")
        and not lower.endswith(("ss", "us", "is"))
    ):
        return lower[:-1]
    return lower


def _candidate_key(surface: str) -> str:
    normalized = unicodedata.normalize("NFKC", surface).replace("’", "'")
    words = [word.casefold() for word in _WORD_PATTERN.findall(normalized)]
    if words:
        words[-1] = _singularize_word(words[-1])
    return " ".join(words)


def _is_title_token(token: str) -> bool:
    letters = "".join(character for character in token if character.isalpha())
    return bool(letters) and (
        (letters.isupper() and len(letters) >= 2)
        or (token[0].isupper() and any(character.islower() for character in token[1:]))
    )


def _is_title_phrase(words: list[str]) -> bool:
    content = [word for word in words if word.casefold() not in _LOWER_CONNECTORS]
    return bool(content) and all(
        _is_title_token(word) or word.casefold() in _LOWER_CONNECTORS
        for word in words
    )


def _clean_example(text: str, limit: int = 240) -> str:
    clean = " ".join(text.replace("\t", " ").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _block_location(block: SourceBlock) -> str:
    if block.source_type == "pdf":
        return f"p{block.page}"
    prefix = IMAGE_SOURCE_ROOT + "/"
    return block.source_file[len(prefix) :] if block.source_file.startswith(prefix) else block.source_file


def _collect_occurrences(
    blocks: list[SourceBlock], *, max_words: int
) -> dict[str, list[_Occurrence]]:
    grouped: dict[str, list[_Occurrence]] = defaultdict(list)
    seen: set[tuple[str, int, int, int, str]] = set()
    for block in blocks:
        enumerated_label = bool(_LIST_ENUMERATOR_PATTERN.match(block.effective_text))
        text = _LIST_ENUMERATOR_PATTERN.sub("", block.effective_text)
        text = _QUANTITY_PREFIX_PATTERN.sub(" ", text)
        text = _TOKEN_PATTERN.sub(" ", text)
        example = _clean_example(block.effective_text)
        block_type = block.block_type.casefold()
        segments = _SEGMENT_SPLIT_PATTERN.split(text)
        primary_segment_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if _WORD_PATTERN.search(segment)
            ),
            None,
        )
        for segment_index, segment in enumerate(segments):
            words = _WORD_PATTERN.findall(segment)
            if not words:
                continue
            whole_heading = (
                (
                    block_type in _HEADING_TYPES
                    or (
                        block_type == "list_item"
                        and (
                            ":" in block.effective_text
                            or enumerated_label
                        )
                    )
                )
                and segment_index == primary_segment_index
                and len(words) <= 6
            )
            for start in range(len(words)):
                for length in range(1, min(max_words, len(words) - start) + 1):
                    phrase_words = words[start : start + length]
                    surface = " ".join(phrase_words)
                    key = _candidate_key(surface)
                    if not key:
                        continue
                    identity = (block.id, segment_index, start, start + length, key)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    grouped[key].append(
                        _Occurrence(
                            surface=surface,
                            block_id=block.id,
                            source_order=block.source_order,
                            location=_block_location(block),
                            example=example,
                            block_type=block_type,
                            segment_index=segment_index,
                            start=start,
                            end=start + length,
                            heading_evidence=whole_heading
                            and start == 0
                            and length == len(words),
                            title_evidence=_is_title_phrase(phrase_words)
                            and (start > 0 or whole_heading),
                        )
                    )
    return grouped


def _representative_surface(occurrences: list[_Occurrence]) -> str:
    counts = Counter(item.surface for item in occurrences)
    return sorted(
        counts,
        key=lambda value: (-counts[value], len(value.split()), len(value), value.casefold(), value),
    )[0]


def _infer_category(source_term: str, occurrences: list[_Occurrence]) -> str:
    words = {word.casefold() for word in _WORD_PATTERN.findall(source_term)}
    block_types = {item.block_type for item in occurrences}
    if "ability" in block_types:
        return "ability"
    if words & _COMPONENT_WORDS:
        return "component"
    if block_types & {"identifier", "label", "ui"}:
        return "ui"
    if _is_title_phrase(_WORD_PATTERN.findall(source_term)):
        return "proper_noun"
    if len(words) > 1:
        return "phrase"
    return "term"


def _qualifies(
    key: str,
    occurrences: list[_Occurrence],
    *,
    min_frequency: int,
) -> bool:
    words = key.split()
    if not words or len(key) < 3:
        return False
    if len(words) == 1 and re.fullmatch(r"[ivxlcdm]+", words[0], re.IGNORECASE):
        return False
    if len(words) == 1 and words[0] in _STOPWORDS:
        return False
    if len(words) > 1 and words[0] in {"a", "an", "the"}:
        return False
    heading_evidence = any(item.heading_evidence for item in occurrences)
    title_evidence = any(item.title_evidence for item in occurrences)
    if len(words) > 1 and not (heading_evidence or title_evidence):
        if words[0] in _STOPWORDS or words[-1] in _STOPWORDS:
            return False
        if sum(word in _STOPWORDS for word in words) > len(words) // 2:
            return False
    block_count = len({item.block_id for item in occurrences})
    frequency = len(occurrences)
    if heading_evidence:
        return True
    if len(words) > 1 and title_evidence:
        return block_count >= 2
    if len(words) == 1 and title_evidence and block_count >= 2:
        return True
    return frequency >= min_frequency and block_count >= 2


def _contains_words(container: str, value: str) -> bool:
    container_words = container.split()
    value_words = value.split()
    if len(container_words) <= len(value_words):
        return False
    return any(
        container_words[index : index + len(value_words)] == value_words
        for index in range(len(container_words) - len(value_words) + 1)
    )


def _is_covered_by_longer_candidate(
    occurrence: _Occurrence,
    longer_occurrences: list[_Occurrence],
) -> bool:
    return any(
        candidate.block_id == occurrence.block_id
        and candidate.segment_index == occurrence.segment_index
        and candidate.start <= occurrence.start
        and candidate.end >= occurrence.end
        for candidate in longer_occurrences
    )


def _prune_fully_nested_candidates(
    candidates: list[tuple[str, GlossaryCandidate]],
    grouped: dict[str, list[_Occurrence]],
) -> list[GlossaryCandidate]:
    candidate_keys = {key for key, _ in candidates}
    result: list[GlossaryCandidate] = []
    for key, candidate in candidates:
        longer_keys = [
            longer
            for longer in candidate_keys
            if _contains_words(longer, key)
        ]
        if longer_keys and all(
            any(
                _is_covered_by_longer_candidate(occurrence, grouped[longer])
                for longer in longer_keys
            )
            for occurrence in grouped[key]
        ):
            continue
        result.append(candidate)
    return result


def extract_glossary_candidates(
    blocks: list[SourceBlock],
    *,
    min_frequency: int = 2,
    max_words: int = 4,
    max_candidates: int = 500,
) -> list[GlossaryCandidate]:
    if min_frequency <= 0:
        raise GlossaryBuildError("min_frequency must be greater than zero.")
    if not 1 <= max_words <= 6:
        raise GlossaryBuildError("max_words must be between 1 and 6.")
    if max_candidates <= 0:
        raise GlossaryBuildError("max_candidates must be greater than zero.")
    grouped = _collect_occurrences(blocks, max_words=max_words)
    candidates: list[tuple[str, GlossaryCandidate]] = []
    for key, occurrences in grouped.items():
        if not _qualifies(key, occurrences, min_frequency=min_frequency):
            continue
        source_term = _representative_surface(occurrences)
        variants = tuple(
            sorted(
                {item.surface for item in occurrences},
                key=lambda value: (value.casefold(), value),
            )
        )
        ordered = sorted(occurrences, key=lambda item: item.source_order)
        locations = tuple(dict.fromkeys(item.location for item in ordered))
        block_count = len({item.block_id for item in occurrences})
        heading_bonus = 3.0 if any(item.heading_evidence for item in occurrences) else 0.0
        title_bonus = 1.5 if any(item.title_evidence for item in occurrences) else 0.0
        score = (
            math.log2(len(occurrences) + 1) * 2
            + min(block_count, 8) * 0.5
            + heading_bonus
            + title_bonus
            + (0.5 if len(key.split()) > 1 else 0.0)
        )
        candidate_id = "term-" + _sha256_bytes(key.encode("utf-8"))[:12]
        candidates.append(
            (
                key,
                GlossaryCandidate(
                candidate_id=candidate_id,
                source_term=source_term,
                category=_infer_category(source_term, occurrences),
                variants=variants,
                occurrences=len(occurrences),
                locations=locations,
                example=ordered[0].example,
                score=round(score, 3),
                ),
            )
        )
    pruned = _prune_fully_nested_candidates(candidates, grouped)
    return sorted(
        pruned,
        key=lambda item: (-item.score, -item.occurrences, item.source_term.casefold()),
    )[:max_candidates]


def _render_review_tsv(candidates: list[GlossaryCandidate]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=GLOSSARY_REVIEW_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for candidate in candidates:
        writer.writerow(candidate.to_review_row())
    return buffer.getvalue().encode("utf-8-sig")


def _read_state(path: Path) -> dict[str, Any] | None:
    return read_json_object(path)


def build_project_glossary_candidates(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
    min_frequency: int = 2,
    max_words: int = 4,
    max_candidates: int = 500,
    force: bool = False,
    dry_run: bool = False,
) -> GlossaryBuildResult:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if not pipeline["final_source_approved"]:
        raise GlossaryBuildError(
            "Final common source is not currently approved. Complete glk review finalize "
            "or resolve stale/modified review files first."
        )
    blocks, approved_data = _load_approved_blocks(location.path)
    approved_hash = _sha256_bytes(approved_data)
    candidates = extract_glossary_candidates(
        blocks,
        min_frequency=min_frequency,
        max_words=max_words,
        max_candidates=max_candidates,
    )
    output_path = paths.glossary_review
    state_path = paths.glossary_build_state
    state = _read_state(state_path)
    parameters = {
        "min_frequency": min_frequency,
        "max_words": max_words,
        "max_candidates": max_candidates,
    }
    state_matches = bool(
        state
        and state.get("status") == "complete"
        and state.get("version") == GLOSSARY_BUILD_VERSION
        and state.get("approved_source_sha256") == approved_hash
        and state.get("parameters") == parameters
    )
    exists = output_path.is_file()
    recoverable_state = bool(
        state
        and state.get("status") in {"writing", "failed"}
        and state.get("version") == GLOSSARY_BUILD_VERSION
        and state.get("approved_source_sha256") == approved_hash
        and state.get("parameters") == parameters
    )
    if recoverable_state and not dry_run:
        output_data = _render_review_tsv(candidates)
        output_hash = _sha256_bytes(output_data)
        if exists and _sha256_bytes(output_path.read_bytes()) != output_hash:
            if not force:
                return GlossaryBuildResult(
                    project_path=str(location.path),
                    approved_source_sha256=approved_hash,
                    candidate_count=len(candidates),
                    output_file=str(output_path),
                    status="stale",
                )
        else:
            if not exists:
                _write_bytes_atomic(output_path, output_data)
            _write_json_atomic(
                state_path,
                {
                    "schema_version": 1,
                    "status": "complete",
                    "version": GLOSSARY_BUILD_VERSION,
                    "input_file": paths.relative(
                        paths.approved_source_segments
                    ),
                    "approved_source_sha256": approved_hash,
                    "parameters": parameters,
                    "candidate_count": len(candidates),
                    "output_file": paths.relative(paths.glossary_review),
                    "baseline_output_sha256": output_hash,
                    "updated_at": _utc_now(),
                },
            )
            return GlossaryBuildResult(
                project_path=str(location.path),
                approved_source_sha256=approved_hash,
                candidate_count=len(candidates),
                output_file=str(output_path),
                status="current",
                created=not exists,
            )
    if exists and not force:
        status = "current" if state_matches else "stale"
        return GlossaryBuildResult(
            project_path=str(location.path),
            approved_source_sha256=approved_hash,
            candidate_count=(
                int(state["candidate_count"])
                if state_matches and state is not None
                else len(candidates)
            ),
            output_file=str(output_path),
            status=status,
            cached=state_matches,
            dry_run=dry_run,
        )

    status = "would_reset" if dry_run and exists else "would_create" if dry_run else "current"
    if dry_run:
        return GlossaryBuildResult(
            project_path=str(location.path),
            approved_source_sha256=approved_hash,
            candidate_count=len(candidates),
            output_file=None,
            status=status,
            created=not exists,
            reset=exists and force,
            dry_run=True,
        )

    output_data = _render_review_tsv(candidates)
    output_hash = _sha256_bytes(output_data)
    _write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "status": "writing",
            "version": GLOSSARY_BUILD_VERSION,
            "input_file": paths.relative(paths.approved_source_segments),
            "approved_source_sha256": approved_hash,
            "parameters": parameters,
            "candidate_count": len(candidates),
            "output_file": paths.relative(paths.glossary_review),
            "baseline_output_sha256": output_hash,
            "updated_at": _utc_now(),
        },
    )
    try:
        _write_bytes_atomic(output_path, output_data)
    except Exception as error:
        try:
            _write_json_atomic(
                state_path,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "version": GLOSSARY_BUILD_VERSION,
                    "input_file": paths.relative(
                        paths.approved_source_segments
                    ),
                    "approved_source_sha256": approved_hash,
                    "parameters": parameters,
                    "candidate_count": len(candidates),
                    "output_file": paths.relative(paths.glossary_review),
                    "baseline_output_sha256": output_hash,
                    "failure_reason": str(error),
                    "updated_at": _utc_now(),
                },
            )
        except OSError:
            pass
        raise
    _write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "status": "complete",
            "version": GLOSSARY_BUILD_VERSION,
            "input_file": paths.relative(paths.approved_source_segments),
            "approved_source_sha256": approved_hash,
            "parameters": parameters,
            "candidate_count": len(candidates),
            "output_file": paths.relative(paths.glossary_review),
            "baseline_output_sha256": output_hash,
            "updated_at": _utc_now(),
        },
    )
    return GlossaryBuildResult(
        project_path=str(location.path),
        approved_source_sha256=approved_hash,
        candidate_count=len(candidates),
        output_file=str(output_path),
        status="current",
        created=not exists,
        reset=exists and force,
    )


def _resolve_review_tsv(file: str | Path, project_path: Path) -> Path:
    candidate = Path(file).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
        raise GlossaryImportError(f"Glossary review TSV not found: {resolved}")

    project_candidate = (project_path / candidate).resolve()
    working_candidate = (Path.cwd() / candidate).resolve()
    if project_candidate.is_file():
        return project_candidate
    if working_candidate.is_file():
        return working_candidate
    raise GlossaryImportError(
        "Glossary review TSV not found. Checked "
        f"{project_candidate} and {working_candidate}."
    )


def _parse_review_tsv(data: bytes) -> list[tuple[int, dict[str, str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise GlossaryImportError("Glossary review TSV must be UTF-8.") from error
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True))
    except csv.Error as error:
        raise GlossaryImportError(f"Invalid glossary review TSV: {error}") from error
    if not records:
        raise GlossaryImportError("Glossary review TSV is empty.")
    if tuple(records[0]) != GLOSSARY_REVIEW_COLUMNS:
        raise GlossaryImportError(
            "Glossary review TSV columns must exactly match: "
            + ", ".join(GLOSSARY_REVIEW_COLUMNS)
        )

    rows: list[tuple[int, dict[str, str]]] = []
    for record_number, record in enumerate(records[1:], start=2):
        if not record or not any(value.strip() for value in record):
            continue
        if len(record) != len(GLOSSARY_REVIEW_COLUMNS):
            raise GlossaryImportError(
                f"Glossary review TSV record {record_number} has {len(record)} fields; "
                f"expected {len(GLOSSARY_REVIEW_COLUMNS)}."
            )
        rows.append((record_number, dict(zip(GLOSSARY_REVIEW_COLUMNS, record))))
    return rows


def _normalized_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _manual_candidate_id(source_term: str) -> str:
    key = _candidate_key(source_term) or _normalized_term(source_term)
    return "manual-" + _sha256_bytes(key.encode("utf-8"))[:12]


def _term_evidence(
    occurrence_index: dict[str, list[_Occurrence]],
    source_term: str,
) -> dict[str, Any] | None:
    words = _WORD_PATTERN.findall(unicodedata.normalize("NFKC", source_term))
    key = _candidate_key(source_term)
    if not words or not key:
        return None
    occurrences = occurrence_index.get(key, [])
    if not occurrences:
        return None
    ordered = sorted(occurrences, key=lambda item: item.source_order)
    return {
        "variants": sorted(
            {item.surface for item in occurrences},
            key=lambda value: (value.casefold(), value),
        ),
        "occurrences": len(occurrences),
        "block_ids": list(dict.fromkeys(item.block_id for item in ordered)),
        "locations": list(dict.fromkeys(item.location for item in ordered)),
        "example": ordered[0].example,
    }


def _render_imported_review_tsv(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=GLOSSARY_REVIEW_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _prepare_glossary_import(
    project: str | Path,
    workspace_root: str | Path,
) -> _GlossaryImportContext:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if not pipeline["final_source_approved"]:
        raise GlossaryImportError(
            "Final common source is not currently approved. Complete glk review finalize "
            "or resolve stale/modified review files first."
        )
    if pipeline["glossary_status"] != "current":
        raise GlossaryImportError(
            "Glossary review is not current. Run glk glossary build and resolve any "
            "stale review TSV before importing."
        )

    blocks, approved_data = _load_approved_blocks(location.path)
    approved_hash = _sha256_bytes(approved_data)
    build_state = _read_state(paths.glossary_build_state)
    if (
        not build_state
        or build_state.get("status") != "complete"
        or build_state.get("version") != GLOSSARY_BUILD_VERSION
        or build_state.get("approved_source_sha256") != approved_hash
    ):
        raise GlossaryImportError(
            "Glossary build state is missing or incompatible. Rebuild the review TSV."
        )
    parameters = build_state.get("parameters")
    if not isinstance(parameters, dict):
        raise GlossaryImportError("Glossary build parameters are missing.")
    try:
        expected_candidates = extract_glossary_candidates(
            blocks,
            min_frequency=parameters["min_frequency"],
            max_words=parameters["max_words"],
            max_candidates=parameters["max_candidates"],
        )
    except (KeyError, TypeError, GlossaryBuildError) as error:
        raise GlossaryImportError(
            "Glossary build parameters are invalid. Rebuild the review TSV."
        ) from error
    return _GlossaryImportContext(
        project_path=location.path,
        project_id=location.manifest.project_id,
        source_language=location.manifest.source_language,
        target_language=location.manifest.target_language,
        paths=paths,
        blocks=tuple(blocks),
        approved_hash=approved_hash,
        expected_candidate_ids=frozenset(
            candidate.candidate_id for candidate in expected_candidates
        ),
    )


def _normalize_glossary_row_fields(
    record_number: int,
    raw_row: dict[str, str],
) -> _GlossaryRowFields:
    status = raw_row["status"].strip()
    source_term = " ".join(raw_row["source_term"].split())
    translation = raw_row["translation"].strip()
    category = raw_row["category"].strip()
    note = raw_row["note"].strip()
    candidate_id = raw_row["candidate_id"].strip()
    if status not in _GLOSSARY_STATUSES:
        raise GlossaryImportError(
            f"Record {record_number} has invalid status {status!r}."
        )
    if status == "review":
        raise GlossaryImportError(
            f"Record {record_number} ({source_term or 'empty term'}) is still in review."
        )
    if not source_term:
        raise GlossaryImportError(
            f"Record {record_number} has an empty source_term."
        )
    if (
        "{" in source_term
        or "}" in source_term
        or _TOKEN_PATTERN.search(source_term)
    ):
        raise GlossaryImportError(
            f"Record {record_number} registers a protected token as a glossary term."
        )
    if category not in _GLOSSARY_CATEGORIES:
        raise GlossaryImportError(
            f"Record {record_number} has invalid category {category!r}."
        )
    if status == "approved" and not translation:
        raise GlossaryImportError(
            f"Record {record_number} is approved but translation is empty."
        )
    if status == "keep":
        if (
            translation
            and _normalized_term(translation) != _normalized_term(source_term)
        ):
            raise GlossaryImportError(
                f"Record {record_number} is keep but translation differs from source_term."
            )
        translation = source_term
    return _GlossaryRowFields(
        status,
        source_term,
        translation,
        category,
        note,
        candidate_id,
    )


def _register_glossary_row_identity(
    record_number: int,
    fields: _GlossaryRowFields,
    *,
    expected_candidate_ids: frozenset[str],
    tracker: _GlossaryImportTracker,
) -> tuple[str, str]:
    normalized_source = _normalized_term(fields.source_term)
    if normalized_source in tracker.seen_terms:
        raise GlossaryImportError(
            f"Record {record_number} duplicates source_term from record "
            f"{tracker.seen_terms[normalized_source]}."
        )
    tracker.seen_terms[normalized_source] = record_number
    source_key = _candidate_key(fields.source_term)
    if source_key and source_key in tracker.seen_term_keys:
        raise GlossaryImportError(
            f"Record {record_number} duplicates a case/plural variant from record "
            f"{tracker.seen_term_keys[source_key]}."
        )
    if source_key:
        tracker.seen_term_keys[source_key] = record_number

    candidate_id = fields.candidate_id
    if candidate_id in expected_candidate_ids:
        origin = "auto"
        tracker.seen_automatic_ids.add(candidate_id)
    elif candidate_id.startswith("manual-"):
        expected_manual_id = _manual_candidate_id(fields.source_term)
        if candidate_id != expected_manual_id:
            raise GlossaryImportError(
                f"Record {record_number} has a changed manual candidate_id. "
                "Clear candidate_id to add the edited term as a new manual row."
            )
        origin = "manual"
    elif candidate_id:
        raise GlossaryImportError(
            f"Record {record_number} has unknown or changed candidate_id "
            f"{candidate_id!r}."
        )
    else:
        origin = "manual"
        candidate_id = _manual_candidate_id(fields.source_term)
    if candidate_id in tracker.seen_ids:
        raise GlossaryImportError(
            f"Record {record_number} duplicates candidate_id from record "
            f"{tracker.seen_ids[candidate_id]}."
        )
    tracker.seen_ids[candidate_id] = record_number
    return candidate_id, origin


def _resolve_glossary_row_evidence(
    record_number: int,
    source_term: str,
    origin: str,
    *,
    occurrence_index: dict[str, list[_Occurrence]],
    allow_missing_terms: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    evidence = _term_evidence(occurrence_index, source_term)
    if evidence is not None:
        return evidence, True, None
    if origin != "manual" or not allow_missing_terms:
        raise GlossaryImportError(
            f"Record {record_number} source_term {source_term!r} was not found "
            "in the approved source."
        )
    return (
        {
            "variants": [source_term],
            "occurrences": 0,
            "block_ids": [],
            "locations": [],
            "example": "",
        },
        False,
        f"Manual term {source_term!r} was imported without source evidence.",
    )


def _normalize_glossary_import_row(
    record_number: int,
    raw_row: dict[str, str],
    *,
    expected_candidate_ids: frozenset[str],
    occurrence_index: dict[str, list[_Occurrence]],
    tracker: _GlossaryImportTracker,
    allow_missing_terms: bool,
) -> tuple[dict[str, str], dict[str, Any], str | None]:
    fields = _normalize_glossary_row_fields(record_number, raw_row)
    candidate_id, origin = _register_glossary_row_identity(
        record_number,
        fields,
        expected_candidate_ids=expected_candidate_ids,
        tracker=tracker,
    )
    evidence, source_verified, warning = _resolve_glossary_row_evidence(
        record_number,
        fields.source_term,
        origin,
        occurrence_index=occurrence_index,
        allow_missing_terms=allow_missing_terms,
    )

    normalized_row = {
        "status": fields.status,
        "source_term": fields.source_term,
        "translation": fields.translation,
        "category": fields.category,
        "note": fields.note,
        "variants": " | ".join(evidence["variants"]),
        "occurrences": str(evidence["occurrences"]),
        "locations": ",".join(evidence["locations"]),
        "example": evidence["example"],
        "candidate_id": candidate_id,
    }
    entry = {
        "candidate_id": candidate_id,
        "source_term": fields.source_term,
        "translation": fields.translation,
        "category": fields.category,
        "status": fields.status,
        "note": fields.note,
        "variants": evidence["variants"],
        "occurrences": evidence["occurrences"],
        "block_ids": evidence["block_ids"],
        "locations": evidence["locations"],
        "example": evidence["example"],
        "origin": origin,
        "source_verified": source_verified,
    }
    return normalized_row, entry, warning


def _normalize_glossary_import(
    context: _GlossaryImportContext,
    input_rows: list[tuple[int, dict[str, str]]],
    *,
    allow_missing_terms: bool,
) -> _GlossaryImportPayload:
    evidence_max_words = max(
        (
            len(
                _WORD_PATTERN.findall(
                    unicodedata.normalize("NFKC", raw_row["source_term"])
                )
            )
            for _record_number, raw_row in input_rows
        ),
        default=1,
    )
    occurrence_index = _collect_occurrences(
        list(context.blocks),
        max_words=max(evidence_max_words, 1),
    )
    tracker = _GlossaryImportTracker({}, {}, {}, set())
    normalized_rows: list[dict[str, str]] = []
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for record_number, raw_row in input_rows:
        normalized_row, entry, warning = _normalize_glossary_import_row(
            record_number,
            raw_row,
            expected_candidate_ids=context.expected_candidate_ids,
            occurrence_index=occurrence_index,
            tracker=tracker,
            allow_missing_terms=allow_missing_terms,
        )
        normalized_rows.append(normalized_row)
        entries.append(entry)
        if warning is not None:
            warnings.append(warning)

    missing_candidate_ids = sorted(
        context.expected_candidate_ids - tracker.seen_automatic_ids
    )
    if missing_candidate_ids:
        preview = ", ".join(missing_candidate_ids[:5])
        suffix = "..." if len(missing_candidate_ids) > 5 else ""
        raise GlossaryImportError(
            "Glossary review TSV deleted generated candidate IDs: "
            f"{preview}{suffix}. Mark unwanted rows as rejected instead of deleting them."
        )

    normalized_tsv = _render_imported_review_tsv(normalized_rows)
    return _GlossaryImportPayload(
        normalized_tsv=normalized_tsv,
        review_hash=_sha256_bytes(normalized_tsv),
        entries=tuple(entries),
        warnings=tuple(warnings),
    )


def _glossary_import_is_cached(
    context: _GlossaryImportContext,
    payload: _GlossaryImportPayload,
) -> bool:
    paths = context.paths
    import_state = _read_state(paths.glossary_import_state)
    return bool(
        import_state
        and import_state.get("status") == "complete"
        and import_state.get("version") == GLOSSARY_IMPORT_VERSION
        and import_state.get("approved_source_sha256")
        == context.approved_hash
        and import_state.get("review_tsv_sha256") == payload.review_hash
        and paths.glossary_review.is_file()
        and _sha256_bytes(paths.glossary_review.read_bytes())
        == payload.review_hash
        and paths.termbase.is_file()
        and import_state.get("termbase_sha256")
        == _sha256_bytes(paths.termbase.read_bytes())
    )


def _glossary_import_result(
    context: _GlossaryImportContext,
    payload: _GlossaryImportPayload,
    *,
    cached: bool = False,
) -> GlossaryImportResult:
    return GlossaryImportResult(
        project_path=str(context.project_path),
        approved_source_sha256=context.approved_hash,
        review_tsv_sha256=payload.review_hash,
        entry_count=len(payload.entries),
        active_count=payload.active_count,
        rejected_count=payload.rejected_count,
        manual_count=payload.manual_count,
        unverified_count=payload.unverified_count,
        review_file=str(context.paths.glossary_review),
        output_file=str(context.paths.termbase),
        cached=cached,
        warnings=payload.warnings,
    )


def _write_glossary_import(
    context: _GlossaryImportContext,
    payload: _GlossaryImportPayload,
) -> None:
    paths = context.paths
    generated_at = _utc_now()
    termbase = {
        "schema_version": 1,
        "version": GLOSSARY_IMPORT_VERSION,
        "project_id": context.project_id,
        "source_language": context.source_language,
        "target_language": context.target_language,
        "approved_source_sha256": context.approved_hash,
        "review_tsv_sha256": payload.review_hash,
        "generated_at": generated_at,
        "entries": payload.entries,
    }
    termbase_data = (
        json.dumps(termbase, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    termbase_hash = _sha256_bytes(termbase_data)
    _write_bytes_atomic(paths.glossary_review, payload.normalized_tsv)
    _write_bytes_atomic(paths.termbase, termbase_data)
    _write_json_atomic(
        paths.glossary_import_state,
        {
            "schema_version": 1,
            "status": "complete",
            "version": GLOSSARY_IMPORT_VERSION,
            "input_file": paths.relative(paths.glossary_review),
            "approved_source_sha256": context.approved_hash,
            "review_tsv_sha256": payload.review_hash,
            "output_file": paths.relative(paths.termbase),
            "termbase_sha256": termbase_hash,
            "entry_count": len(payload.entries),
            "active_count": payload.active_count,
            "rejected_count": payload.rejected_count,
            "manual_count": payload.manual_count,
            "unverified_count": payload.unverified_count,
            "candidate_ids": [
                item["candidate_id"] for item in payload.entries
            ],
            "updated_at": generated_at,
        },
    )


def import_project_glossary(
    *,
    project: str | Path,
    file: str | Path,
    workspace_root: str | Path = "workspaces",
    allow_missing_terms: bool = False,
) -> GlossaryImportResult:
    context = _prepare_glossary_import(project, workspace_root)
    input_path = _resolve_review_tsv(file, context.project_path)
    input_rows = _parse_review_tsv(input_path.read_bytes())
    payload = _normalize_glossary_import(
        context,
        input_rows,
        allow_missing_terms=allow_missing_terms,
    )

    if _glossary_import_is_cached(context, payload):
        return _glossary_import_result(context, payload, cached=True)
    _write_glossary_import(context, payload)
    return _glossary_import_result(context, payload)
