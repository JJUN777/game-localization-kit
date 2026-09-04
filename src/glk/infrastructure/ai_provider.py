"""Select and construct the configured AI provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from glk.config import resolve_settings_root
from glk.infrastructure.gemini_common import gemini_failure_code, resolve_model_name
from glk.infrastructure.gemini_glossary_triage import GeminiGlossaryTriageProvider
from glk.infrastructure.gemini_layout import GeminiLayoutProvider
from glk.infrastructure.gemini_ocr import GeminiImageOcrProvider
from glk.infrastructure.gemini_pdf_icon_audit import GeminiPdfIconAuditProvider
from glk.infrastructure.gemini_translation import GeminiTranslationProvider
from glk.infrastructure.gemini_translation_prompt import (
    GeminiTranslationPromptDraftProvider,
)
from glk.infrastructure.openai_common import (
    openai_failure_code,
    resolve_openai_model_name,
)
from glk.infrastructure.openai_glossary_triage import OpenAIGlossaryTriageProvider
from glk.infrastructure.openai_layout import OpenAILayoutProvider
from glk.infrastructure.openai_ocr import OpenAIImageOcrProvider
from glk.infrastructure.openai_pdf_icon_audit import OpenAIPdfIconAuditProvider
from glk.infrastructure.openai_translation import OpenAITranslationProvider
from glk.infrastructure.openai_translation_prompt import (
    OpenAITranslationPromptDraftProvider,
)


AiProviderName = Literal["gemini", "openai"]
DEFAULT_AI_PROVIDER: AiProviderName = "gemini"
AI_PROVIDER_NAMES = frozenset({"gemini", "openai"})


def resolve_ai_provider_name(
    settings_root: str | os.PathLike[str] | None = None,
) -> AiProviderName:
    """Resolve the selected provider, preserving Gemini as the legacy default."""
    environment_value = os.getenv("GLK_AI_PROVIDER", "").strip().lower()
    if environment_value:
        if environment_value not in AI_PROVIDER_NAMES:
            raise ValueError("GLK_AI_PROVIDER must be gemini or openai.")
        return environment_value  # type: ignore[return-value]
    normalized_root = Path(settings_root) if settings_root is not None else None
    parsed = dotenv_values(resolve_settings_root(normalized_root) / ".env")
    file_value = parsed.get("GLK_AI_PROVIDER")
    if isinstance(file_value, str) and file_value.strip():
        normalized = file_value.strip().lower()
        if normalized not in AI_PROVIDER_NAMES:
            raise ValueError("GLK_AI_PROVIDER must be gemini or openai.")
        return normalized  # type: ignore[return-value]
    return DEFAULT_AI_PROVIDER


def resolve_ai_model_name(
    model_name: str | None = None,
    *,
    provider_name: AiProviderName | None = None,
    settings_root: str | os.PathLike[str] | None = None,
) -> str:
    provider = provider_name or resolve_ai_provider_name(settings_root)
    if provider == "openai":
        return resolve_openai_model_name(model_name, settings_root=settings_root)
    return resolve_model_name(model_name, settings_root=settings_root)


def translation_provider_prompt_version(
    provider_name: AiProviderName,
) -> str:
    if provider_name == "openai":
        return OpenAITranslationProvider.prompt_version
    return GeminiTranslationProvider.prompt_version


def glossary_triage_provider_prompt_version(
    provider_name: AiProviderName,
) -> str:
    if provider_name == "openai":
        return OpenAIGlossaryTriageProvider.prompt_version
    return GeminiGlossaryTriageProvider.prompt_version


def translation_prompt_draft_provider_prompt_version(
    provider_name: AiProviderName,
) -> str:
    if provider_name == "openai":
        return OpenAITranslationPromptDraftProvider.prompt_version
    return GeminiTranslationPromptDraftProvider.prompt_version


def create_layout_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAILayoutProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiLayoutProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def create_image_ocr_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAIImageOcrProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiImageOcrProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def create_pdf_icon_audit_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAIPdfIconAuditProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiPdfIconAuditProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def create_glossary_triage_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAIGlossaryTriageProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiGlossaryTriageProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def create_translation_prompt_draft_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAITranslationPromptDraftProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiTranslationPromptDraftProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def create_translation_provider(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
):
    if resolve_ai_provider_name(settings_root) == "openai":
        return OpenAITranslationProvider.from_environment(
            model_name,
            settings_root=settings_root,
        )
    return GeminiTranslationProvider.from_environment(
        model_name,
        settings_root=settings_root,
    )


def ai_failure_code(error: BaseException) -> str:
    """Classify either provider without leaking request or credential details."""
    openai_code = openai_failure_code(error)
    if openai_code != "SOURCE_PROCESSING_FAILED":
        return openai_code
    return gemini_failure_code(error)
