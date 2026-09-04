"""AI-assisted project translation prompt drafting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Protocol, Sequence

from glk.application._io import write_json_atomic
from glk.application._translation_context import load_approved_blocks
from glk.application.ai_usage_ledger import append_ai_usage_event
from glk.application.project_service import load_project
from glk.domain.source_block import SourceBlock
from glk.domain.workspace import WorkspacePaths
from glk.extraction.translation_prompt_draft import (
    TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
    TRANSLATION_PROMPT_DRAFT_VERSION,
    TranslationPromptDraftValidationError,
    build_translation_prompt_draft_request,
    validate_translation_prompt_draft_result,
)
from glk.infrastructure.ai_provider import (
    ai_failure_code,
    create_translation_prompt_draft_provider,
    resolve_ai_model_name,
    resolve_ai_provider_name,
    translation_prompt_draft_provider_prompt_version,
)
from glk.infrastructure.ai_usage import (
    estimate_ai_cost,
    provider_usage,
    usage_delta,
)


TRANSLATION_PROMPT_DRAFT_CACHE_VERSION = 1
OPENING_CONTEXT_GROUPS = 4
MAX_OPENING_CONTEXT_CHARS = 12_000
MAX_LATER_STYLE_BLOCKS = 8
MAX_LATER_STYLE_CHARS = 4_000
MAX_LATER_STYLE_BLOCK_CHARS = 500
MAX_CURRENT_PROMPT_CHARS = 8_000
_ESTIMATED_OUTPUT_TOKENS_LOW = 250
_ESTIMATED_OUTPUT_TOKENS_HIGH = 700


class TranslationPromptAiError(ValueError):
    """Raised when an AI prompt draft cannot be generated safely."""

    code = "TRANSLATION_PROMPT_AI_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class TranslationPromptDraftProvider(Protocol):
    model_name: str
    prompt_version: str

    def generate_draft(self, prompt: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TranslationPromptDraftEstimate:
    provider: str
    model: str
    sample_count: int
    request_count: int
    cached: bool
    cached_result: dict[str, str] | None
    estimated_input_tokens: int
    estimated_output_tokens_low: int
    estimated_output_tokens_high: int
    estimated_cost_usd_low: float | None
    estimated_cost_usd_high: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranslationPromptDraftResult:
    draft: str
    rationale: str
    provider: str
    model: str
    sample_count: int
    cached: bool
    usage: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ProviderIdentity:
    provider: str
    model: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class _DraftInputs:
    project_path: Path
    prompt: str
    samples: list[dict[str, Any]]
    identity: _ProviderIdentity
    fingerprint: str
    source_sha256: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _provider_identity(
    provider: TranslationPromptDraftProvider | None,
    *,
    settings_root: str | Path | None,
) -> _ProviderIdentity:
    if provider is None:
        configured_provider = resolve_ai_provider_name(settings_root)
        return _ProviderIdentity(
            provider=configured_provider,
            model=resolve_ai_model_name(
                provider_name=configured_provider,
                settings_root=settings_root,
            ),
            prompt_version=translation_prompt_draft_provider_prompt_version(
                configured_provider
            ),
        )
    usage = provider_usage(provider)
    usage_provider = usage.get("provider") if usage else None
    adapter_provider = (
        usage_provider
        if isinstance(usage_provider, str) and usage_provider
        else type(provider).__module__.rsplit(".", 1)[-1]
    )
    return _ProviderIdentity(
        provider=adapter_provider,
        model=provider.model_name,
        prompt_version=str(
            getattr(
                provider,
                "prompt_version",
                TRANSLATION_PROMPT_DRAFT_VERSION,
            )
        ),
    )


def _representative_samples(
    blocks: Sequence[SourceBlock],
) -> list[dict[str, Any]]:
    if not blocks:
        raise TranslationPromptAiError("Approved source is empty.")
    ordered = sorted(blocks, key=lambda block: block.source_order)
    opening_group_keys: list[tuple[str, int | None]] = []
    for block in ordered:
        key = (block.source_file, block.page)
        if key not in opening_group_keys:
            opening_group_keys.append(key)
        if len(opening_group_keys) == OPENING_CONTEXT_GROUPS:
            break
    opening_keys = set(opening_group_keys)
    samples: list[dict[str, Any]] = []
    opening_remaining = MAX_OPENING_CONTEXT_CHARS
    for block in ordered:
        if (block.source_file, block.page) not in opening_keys:
            continue
        text = " ".join(block.effective_text.split())
        if not text or opening_remaining <= 0:
            continue
        excerpt = text[:opening_remaining]
        opening_remaining -= len(excerpt)
        samples.append(
            {
                "role": "opening_context",
                "type": block.block_type,
                "page": block.page,
                "source": excerpt,
            }
        )
    later = [
        block
        for block in ordered
        if (block.source_file, block.page) not in opening_keys
        and block.effective_text.strip()
    ]
    slots = min(MAX_LATER_STYLE_BLOCKS, len(later))
    later_indices = [
        (
            0
            if slots == 1
            else round(position * (len(later) - 1) / (slots - 1))
        )
        for position in range(slots)
    ]
    later_remaining = MAX_LATER_STYLE_CHARS
    for index in dict.fromkeys(later_indices):
        block = later[index]
        text = " ".join(block.effective_text.split())
        if later_remaining <= 0:
            break
        excerpt = text[
            : min(MAX_LATER_STYLE_BLOCK_CHARS, later_remaining)
        ]
        later_remaining -= len(excerpt)
        samples.append(
            {
                "role": "later_style_sample",
                "type": block.block_type,
                "page": block.page,
                "source": excerpt,
            }
        )
    if not samples:
        raise TranslationPromptAiError("Approved source has no readable text.")
    return samples


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != TRANSLATION_PROMPT_DRAFT_CACHE_VERSION
    ):
        return None
    return value


def _cached_result(
    cache: dict[str, Any] | None,
    *,
    inputs: _DraftInputs,
) -> dict[str, str] | None:
    if cache is None or cache.get("fingerprint") != inputs.fingerprint:
        return None
    try:
        return validate_translation_prompt_draft_result(cache.get("result"))
    except TranslationPromptDraftValidationError:
        return None


def _prepare_inputs(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    current_prompt: str,
    provider: TranslationPromptDraftProvider | None,
) -> _DraftInputs:
    if not isinstance(current_prompt, str) or not current_prompt.strip():
        raise TranslationPromptAiError("Current translation prompt is empty.")
    location = load_project(project, workspace_root)
    blocks, source_data = load_approved_blocks(location.path)
    samples = _representative_samples(blocks)
    identity = _provider_identity(provider, settings_root=settings_root)
    bounded_prompt = current_prompt.strip()[:MAX_CURRENT_PROMPT_CHARS]
    prompt = build_translation_prompt_draft_request(
        project_name=location.manifest.name,
        source_format=blocks[0].source_type,
        source_language=location.manifest.source_language,
        target_language=location.manifest.target_language,
        current_prompt=bounded_prompt,
        samples=samples,
    )
    source_sha256 = hashlib.sha256(source_data).hexdigest()
    fingerprint_value = {
        "prompt_version": identity.prompt_version,
        "provider": identity.provider,
        "model": identity.model,
        "source_sha256": source_sha256,
        "current_prompt_sha256": hashlib.sha256(
            current_prompt.strip().encode("utf-8")
        ).hexdigest(),
        "request": prompt,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            fingerprint_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _DraftInputs(
        project_path=location.path,
        prompt=prompt,
        samples=samples,
        identity=identity,
        fingerprint=fingerprint,
        source_sha256=source_sha256,
    )


def estimate_translation_prompt_draft(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    current_prompt: str,
    provider: TranslationPromptDraftProvider | None = None,
    force: bool = False,
) -> TranslationPromptDraftEstimate:
    """Estimate one bounded prompt-draft request without calling AI."""
    inputs = _prepare_inputs(
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        current_prompt=current_prompt,
        provider=provider,
    )
    cache = _load_cache(
        WorkspacePaths(inputs.project_path).translation_prompt_ai_draft_state
    )
    cached_result = None if force else _cached_result(cache, inputs=inputs)
    cached = cached_result is not None
    input_tokens = math.ceil(
        len(
            (
                TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION
                + "\n"
                + inputs.prompt
            ).encode("utf-8")
        )
        / 4
    )
    output_low = 0 if cached else _ESTIMATED_OUTPUT_TOKENS_LOW
    output_high = 0 if cached else _ESTIMATED_OUTPUT_TOKENS_HIGH
    return TranslationPromptDraftEstimate(
        provider=inputs.identity.provider,
        model=inputs.identity.model,
        sample_count=len(inputs.samples),
        request_count=0 if cached else 1,
        cached=cached,
        cached_result=cached_result,
        estimated_input_tokens=0 if cached else input_tokens,
        estimated_output_tokens_low=output_low,
        estimated_output_tokens_high=output_high,
        estimated_cost_usd_low=(
            0.0
            if cached
            else estimate_ai_cost(
                inputs.identity.model,
                input_tokens=input_tokens,
                output_tokens=output_low,
            )
        ),
        estimated_cost_usd_high=(
            0.0
            if cached
            else estimate_ai_cost(
                inputs.identity.model,
                input_tokens=math.ceil(input_tokens * 1.2),
                output_tokens=output_high,
            )
        ),
    )


def generate_translation_prompt_draft(
    *,
    project: str | Path,
    workspace_root: str | Path,
    settings_root: str | Path | None,
    current_prompt: str,
    provider: TranslationPromptDraftProvider | None = None,
    force: bool = False,
) -> TranslationPromptDraftResult:
    """Generate or reuse one editable translation style prompt draft."""
    inputs = _prepare_inputs(
        project=project,
        workspace_root=workspace_root,
        settings_root=settings_root,
        current_prompt=current_prompt,
        provider=provider,
    )
    paths = WorkspacePaths(inputs.project_path)
    cache = _load_cache(paths.translation_prompt_ai_draft_state)
    cached = None if force else _cached_result(cache, inputs=inputs)
    if cached is not None:
        return TranslationPromptDraftResult(
            **cached,
            provider=inputs.identity.provider,
            model=inputs.identity.model,
            sample_count=len(inputs.samples),
            cached=True,
            usage=None,
        )

    active_provider = provider
    usage_before = provider_usage(active_provider) if active_provider else None
    try:
        active_provider = active_provider or create_translation_prompt_draft_provider(
            settings_root=settings_root
        )
        usage_before = provider_usage(active_provider)
        result = validate_translation_prompt_draft_result(
            active_provider.generate_draft(inputs.prompt)
        )
    except Exception as error:
        usage = usage_delta(
            usage_before,
            provider_usage(active_provider) if active_provider else None,
        )
        append_ai_usage_event(
            inputs.project_path,
            stage="translation",
            operation="prompt_draft",
            status="failed",
            usage=usage,
            context={"sample_count": len(inputs.samples)},
        )
        code = (
            "TRANSLATION_PROMPT_AI_RESPONSE_INVALID"
            if isinstance(error, TranslationPromptDraftValidationError)
            else ai_failure_code(error)
        )
        raise TranslationPromptAiError(
            "Could not generate an AI translation prompt draft.",
            code=code,
        ) from error

    usage = usage_delta(usage_before, provider_usage(active_provider))
    append_ai_usage_event(
        inputs.project_path,
        stage="translation",
        operation="prompt_draft",
        usage=usage,
        context={"sample_count": len(inputs.samples)},
    )
    write_json_atomic(
        paths.translation_prompt_ai_draft_state,
        {
            "schema_version": TRANSLATION_PROMPT_DRAFT_CACHE_VERSION,
            "fingerprint": inputs.fingerprint,
            "prompt_version": inputs.identity.prompt_version,
            "provider": inputs.identity.provider,
            "model": inputs.identity.model,
            "source_sha256": inputs.source_sha256,
            "generated_at": _utc_now(),
            "result": result,
        },
    )
    return TranslationPromptDraftResult(
        **result,
        provider=inputs.identity.provider,
        model=inputs.identity.model,
        sample_count=len(inputs.samples),
        cached=False,
        usage=usage,
    )
