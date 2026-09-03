"""AI-assisted first-pass triage for generated glossary candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Protocol, Sequence

from glk.application._io import write_json_atomic
from glk.application.ai_usage_ledger import append_ai_usage_event
from glk.application.glossary_review_service import (
    GLOSSARY_REVIEW_CATEGORIES,
    GLOSSARY_REVIEW_STATUSES,
    GlossaryReviewConflictError,
    GlossaryReviewError,
    get_project_glossary_review_document,
)
from glk.application.glossary_service import (
    GlossaryImportError,
    load_project_glossary_candidate_baseline,
)
from glk.application.project_service import load_project
from glk.application.review_types import GlossaryReviewDocument
from glk.domain.workspace import WorkspacePaths
from glk.extraction.glossary_triage import (
    GLOSSARY_TRIAGE_PROMPT_VERSION,
    GlossaryTriageValidationError,
    build_glossary_triage_prompt,
    validate_glossary_triage_result,
)
from glk.infrastructure.ai_provider import (
    ai_failure_code,
    create_glossary_triage_provider,
    glossary_triage_provider_prompt_version,
    resolve_ai_model_name,
    resolve_ai_provider_name,
)
from glk.infrastructure.ai_usage import (
    estimate_ai_cost,
    provider_usage,
    usage_delta,
)


GLOSSARY_AI_CACHE_VERSION = 1
GLOSSARY_AI_CHUNK_SIZE = 25
MAX_GLOSSARY_AI_CANDIDATES = 500
_ESTIMATED_OUTPUT_TOKENS_LOW = 35
_ESTIMATED_OUTPUT_TOKENS_HIGH = 95


class GlossaryAiTriageError(GlossaryReviewError):
    """Raised when glossary candidates cannot be triaged safely."""

    code = "GLOSSARY_AI_TRIAGE_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class GlossaryTriageProvider(Protocol):
    model_name: str
    prompt_version: str

    def triage(self, prompt: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class GlossaryAiEstimate:
    provider: str
    model: str
    target_count: int
    cached_count: int
    request_count: int
    estimated_input_tokens: int
    estimated_output_tokens_low: int
    estimated_output_tokens_high: int
    estimated_cost_usd_low: float | None
    estimated_cost_usd_high: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GlossaryAiTriageResult:
    suggestions: list[dict[str, Any]]
    target_count: int
    cached_count: int
    usage: dict[str, Any] | None
    provider: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ProviderIdentity:
    provider: str
    model: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    candidate_id: str
    source_term: str
    variants: str
    occurrences: str
    locations: str
    example: str
    current_status: str
    current_translation: str
    current_category: str
    baseline_category: str

    def prompt_value(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_term": self.source_term,
            "variants": self.variants,
            "occurrences": self.occurrences,
            "locations": self.locations,
            "example": self.example,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_cache(path: Path) -> dict[str, Any]:
    empty = {"schema_version": GLOSSARY_AI_CACHE_VERSION, "suggestions": {}}
    if not path.is_file():
        return empty
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return empty
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != GLOSSARY_AI_CACHE_VERSION
        or not isinstance(value.get("suggestions"), dict)
    ):
        return empty
    return value


def _provider_identity(
    provider: GlossaryTriageProvider | None,
    *,
    settings_root: str | Path | None,
) -> _ProviderIdentity:
    if provider is None:
        provider_name = resolve_ai_provider_name(settings_root)
        return _ProviderIdentity(
            provider=provider_name,
            model=resolve_ai_model_name(
                provider_name=provider_name,
                settings_root=settings_root,
            ),
            prompt_version=glossary_triage_provider_prompt_version(
                provider_name
            ),
        )
    usage = provider_usage(provider)
    injected_name = usage.get("provider") if usage else None
    if not isinstance(injected_name, str) or not injected_name:
        injected_name = type(provider).__module__.rsplit(".", 1)[-1]
    prompt_version = getattr(
        provider,
        "prompt_version",
        GLOSSARY_TRIAGE_PROMPT_VERSION,
    )
    return _ProviderIdentity(
        provider=injected_name,
        model=provider.model_name,
        prompt_version=str(prompt_version),
    )


def _candidate_fingerprint(
    candidate: _CandidateRecord,
    *,
    identity: _ProviderIdentity,
    source_language: str,
    target_language: str,
) -> str:
    value = {
        "prompt_version": identity.prompt_version,
        "provider": identity.provider,
        "model": identity.model,
        "source_language": source_language,
        "target_language": target_language,
        "candidate": candidate.prompt_value(),
    }
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _clean_browser_value(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise GlossaryAiTriageError(f"{field} must be a string.")
    return value.strip()


def _candidate_records(
    *,
    project: str | Path,
    workspace_root: str | Path,
    expected_review_sha256: str,
    rows: list[dict[str, Any]],
) -> tuple[GlossaryReviewDocument, list[_CandidateRecord]]:
    document = get_project_glossary_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    if document["review_sha256"] != expected_review_sha256:
        raise GlossaryReviewConflictError(
            "Glossary review changed after this page was loaded. Reload before using AI."
        )
    if not isinstance(rows, list):
        raise GlossaryAiTriageError("rows must be a list.")
    current_by_id = {
        row["candidate_id"]: row
        for row in document["rows"]
        if row["candidate_id"] and not row["manual"]
    }
    try:
        baseline = load_project_glossary_candidate_baseline(
            project=project,
            workspace_root=workspace_root,
        )
    except GlossaryImportError as error:
        raise GlossaryAiTriageError(
            "Could not reconstruct the generated glossary candidates."
        ) from error
    baseline_by_id = {item.candidate_id: item for item in baseline}
    seen: set[str] = set()
    records: list[_CandidateRecord] = []
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise GlossaryAiTriageError(f"Row {index} must be an object.")
        candidate_id = _clean_browser_value(
            raw.get("candidate_id", ""),
            field=f"Row {index} candidate_id",
        )
        if not candidate_id or candidate_id.startswith("manual-"):
            continue
        current = current_by_id.get(candidate_id)
        generated = baseline_by_id.get(candidate_id)
        if current is None or generated is None or candidate_id in seen:
            raise GlossaryAiTriageError(
                f"Row {index} has an unknown or duplicate generated candidate."
            )
        seen.add(candidate_id)
        status = _clean_browser_value(
            raw.get("status"),
            field=f"Row {index} status",
        )
        category = _clean_browser_value(
            raw.get("category"),
            field=f"Row {index} category",
        )
        translation = _clean_browser_value(
            raw.get("translation", ""),
            field=f"Row {index} translation",
        )
        if status not in GLOSSARY_REVIEW_STATUSES:
            raise GlossaryAiTriageError(
                f"Row {index} has an invalid status."
            )
        if category not in GLOSSARY_REVIEW_CATEGORIES:
            raise GlossaryAiTriageError(
                f"Row {index} has an invalid category."
            )
        records.append(
            _CandidateRecord(
                candidate_id=candidate_id,
                source_term=current["source_term"],
                variants=current["variants"],
                occurrences=current["occurrences"],
                locations=current["locations"],
                example=current["example"],
                current_status=status,
                current_translation=translation,
                current_category=category,
                baseline_category=generated.category,
            )
        )
    missing = set(current_by_id) - seen
    if missing:
        raise GlossaryAiTriageError(
            "Generated candidates cannot be omitted from an AI triage request."
        )
    return document, records


def _chunks(
    values: Sequence[_CandidateRecord],
) -> list[Sequence[_CandidateRecord]]:
    return [
        values[index : index + GLOSSARY_AI_CHUNK_SIZE]
        for index in range(0, len(values), GLOSSARY_AI_CHUNK_SIZE)
    ]


def _validated_cache_suggestion(
    entry: Any,
    *,
    candidate: _CandidateRecord,
    fingerprint: str,
) -> dict[str, str] | None:
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return None
    try:
        suggestions = validate_glossary_triage_result(
            {"suggestions": [entry.get("suggestion")]},
            candidates=[candidate.prompt_value()],
        )
    except (GlossaryTriageValidationError, TypeError, ValueError):
        return None
    return suggestions[0]


def _public_suggestion(
    suggestion: dict[str, str],
    *,
    candidate: _CandidateRecord,
    cached: bool,
) -> dict[str, Any]:
    apply_status = (
        suggestion["recommended_status"] in {"review", "approved"}
        or not candidate.current_translation
        or (
            suggestion["recommended_status"] == "keep"
            and candidate.current_translation == candidate.source_term
        )
    )
    return {
        **suggestion,
        "source_term": candidate.source_term,
        "current_status": candidate.current_status,
        "current_translation": candidate.current_translation,
        "current_category": candidate.current_category,
        "apply_status": apply_status,
        "apply_translation": not candidate.current_translation,
        "apply_category": candidate.current_category == candidate.baseline_category,
        "cached": cached,
    }


def _prepare_cached_results(
    *,
    records: Sequence[_CandidateRecord],
    cache: dict[str, Any],
    identity: _ProviderIdentity,
    source_language: str,
    target_language: str,
) -> tuple[
    dict[str, dict[str, Any]],
    list[_CandidateRecord],
    dict[str, str],
]:
    cache_items = cache["suggestions"]
    assert isinstance(cache_items, dict)
    cached: dict[str, dict[str, Any]] = {}
    uncached: list[_CandidateRecord] = []
    fingerprints: dict[str, str] = {}
    for candidate in records:
        fingerprint = _candidate_fingerprint(
            candidate,
            identity=identity,
            source_language=source_language,
            target_language=target_language,
        )
        fingerprints[candidate.candidate_id] = fingerprint
        suggestion = _validated_cache_suggestion(
            cache_items.get(candidate.candidate_id),
            candidate=candidate,
            fingerprint=fingerprint,
        )
        if suggestion is None:
            uncached.append(candidate)
        else:
            cached[candidate.candidate_id] = _public_suggestion(
                suggestion,
                candidate=candidate,
                cached=True,
            )
    return cached, uncached, fingerprints


def estimate_project_glossary_ai_triage(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    expected_review_sha256: str,
    rows: list[dict[str, Any]],
    provider: GlossaryTriageProvider | None = None,
) -> GlossaryAiEstimate:
    """Estimate bounded request count, token volume, and uncached cost."""
    document, all_records = _candidate_records(
        project=project,
        workspace_root=workspace_root,
        expected_review_sha256=expected_review_sha256,
        rows=rows,
    )
    targets = [item for item in all_records if item.current_status == "review"]
    if len(targets) > MAX_GLOSSARY_AI_CANDIDATES:
        raise GlossaryAiTriageError("Too many glossary candidates were selected.")
    identity = _provider_identity(provider, settings_root=settings_root)
    location = load_project(project, workspace_root)
    cache = _load_cache(WorkspacePaths(location.path).glossary_ai_review_state)
    cached, uncached, _fingerprints = _prepare_cached_results(
        records=targets,
        cache=cache,
        identity=identity,
        source_language=document["project"]["source_language"],
        target_language=document["project"]["target_language"],
    )
    prompts = [
        build_glossary_triage_prompt(
            source_language=document["project"]["source_language"],
            target_language=document["project"]["target_language"],
            candidates=[item.prompt_value() for item in chunk],
        )
        for chunk in _chunks(uncached)
    ]
    estimated_input_tokens = sum(
        max(1, math.ceil(len(prompt) / 4)) for prompt in prompts
    )
    output_low = len(uncached) * _ESTIMATED_OUTPUT_TOKENS_LOW
    output_high = len(uncached) * _ESTIMATED_OUTPUT_TOKENS_HIGH
    cost_low = estimate_ai_cost(
        identity.model,
        input_tokens=estimated_input_tokens,
        output_tokens=output_low,
    )
    cost_high = estimate_ai_cost(
        identity.model,
        input_tokens=math.ceil(estimated_input_tokens * 1.2),
        output_tokens=output_high,
    )
    if not uncached:
        cost_low = 0.0
        cost_high = 0.0
    return GlossaryAiEstimate(
        provider=identity.provider,
        model=identity.model,
        target_count=len(targets),
        cached_count=len(cached),
        request_count=len(prompts),
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens_low=output_low,
        estimated_output_tokens_high=output_high,
        estimated_cost_usd_low=cost_low,
        estimated_cost_usd_high=cost_high,
    )


def get_project_glossary_ai_suggestions(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    provider: GlossaryTriageProvider | None = None,
) -> GlossaryAiTriageResult:
    """Return still-valid stored suggestions without making an AI request."""
    document = get_project_glossary_review_document(
        project=project,
        workspace_root=workspace_root,
    )
    rows = [dict(row) for row in document["rows"]]
    _document, records = _candidate_records(
        project=project,
        workspace_root=workspace_root,
        expected_review_sha256=document["review_sha256"],
        rows=rows,
    )
    identity = _provider_identity(provider, settings_root=settings_root)
    location = load_project(project, workspace_root)
    cache = _load_cache(WorkspacePaths(location.path).glossary_ai_review_state)
    cached, _uncached, _fingerprints = _prepare_cached_results(
        records=records,
        cache=cache,
        identity=identity,
        source_language=document["project"]["source_language"],
        target_language=document["project"]["target_language"],
    )
    return GlossaryAiTriageResult(
        suggestions=[cached[item.candidate_id] for item in records if item.candidate_id in cached],
        target_count=0,
        cached_count=len(cached),
        usage=None,
        provider=identity.provider,
        model=identity.model,
    )


def triage_project_glossary_candidates(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    expected_review_sha256: str,
    rows: list[dict[str, Any]],
    provider: GlossaryTriageProvider | None = None,
) -> GlossaryAiTriageResult:
    """Triage untouched automatic review rows and cache each suggestion."""
    document, all_records = _candidate_records(
        project=project,
        workspace_root=workspace_root,
        expected_review_sha256=expected_review_sha256,
        rows=rows,
    )
    targets = [item for item in all_records if item.current_status == "review"]
    if not targets:
        raise GlossaryAiTriageError(
            "There are no generated candidates still awaiting review."
        )
    if len(targets) > MAX_GLOSSARY_AI_CANDIDATES:
        raise GlossaryAiTriageError("Too many glossary candidates were selected.")
    identity = _provider_identity(provider, settings_root=settings_root)
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    cache = _load_cache(paths.glossary_ai_review_state)
    cache_items = cache["suggestions"]
    assert isinstance(cache_items, dict)
    cached, uncached, fingerprints = _prepare_cached_results(
        records=targets,
        cache=cache,
        identity=identity,
        source_language=document["project"]["source_language"],
        target_language=document["project"]["target_language"],
    )
    active_provider = provider
    usage_before = provider_usage(active_provider) if active_provider else None
    completed = dict(cached)
    for chunk in _chunks(uncached):
        prompt = build_glossary_triage_prompt(
            source_language=document["project"]["source_language"],
            target_language=document["project"]["target_language"],
            candidates=[item.prompt_value() for item in chunk],
        )
        chunk_usage_before = provider_usage(active_provider) if active_provider else None
        try:
            active_provider = active_provider or create_glossary_triage_provider(
                settings_root=settings_root
            )
            chunk_usage_before = provider_usage(active_provider)
            response = active_provider.triage(prompt)
            validated = validate_glossary_triage_result(
                response,
                candidates=[item.prompt_value() for item in chunk],
            )
        except Exception as error:
            append_ai_usage_event(
                location.path,
                stage="glossary",
                operation="candidate_triage",
                status="failed",
                usage=usage_delta(
                    chunk_usage_before,
                    provider_usage(active_provider) if active_provider else None,
                ),
                context={"candidate_count": len(chunk)},
            )
            code = (
                "GLOSSARY_AI_RESPONSE_INVALID"
                if isinstance(error, GlossaryTriageValidationError)
                else ai_failure_code(error)
            )
            raise GlossaryAiTriageError(
                "Could not complete AI glossary candidate triage.",
                code=code,
            ) from error
        chunk_usage = usage_delta(
            chunk_usage_before,
            provider_usage(active_provider),
        )
        append_ai_usage_event(
            location.path,
            stage="glossary",
            operation="candidate_triage",
            usage=chunk_usage,
            context={"candidate_count": len(chunk)},
        )
        for candidate, suggestion in zip(chunk, validated, strict=True):
            cache_items[candidate.candidate_id] = {
                "fingerprint": fingerprints[candidate.candidate_id],
                "prompt_version": identity.prompt_version,
                "provider": identity.provider,
                "model": identity.model,
                "reviewed_at": _utc_now(),
                "suggestion": suggestion,
            }
            completed[candidate.candidate_id] = _public_suggestion(
                suggestion,
                candidate=candidate,
                cached=False,
            )
        write_json_atomic(paths.glossary_ai_review_state, cache)

    usage = usage_delta(
        usage_before,
        provider_usage(active_provider) if active_provider else None,
    )
    return GlossaryAiTriageResult(
        suggestions=[completed[item.candidate_id] for item in targets],
        target_count=len(targets),
        cached_count=len(cached),
        usage=usage,
        provider=identity.provider,
        model=identity.model,
    )
