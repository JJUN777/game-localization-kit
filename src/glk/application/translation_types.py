"""Shared translation types used by translation application services."""

from __future__ import annotations

from typing import Any, Protocol


DEFAULT_PROJECT_INSTRUCTIONS = """\
Translate into natural Korean suitable for a board game rulebook.
Use concise formal Korean for rules and instructions.
Preserve the source meaning without adding, omitting, summarizing, or explaining.
Keep headings concise and use terminology consistently."""


class TranslationError(ValueError):
    """Raised when source blocks cannot be translated safely."""


class TranslationValidationError(TranslationError):
    """Raised when a model response violates the translation contract."""

    code = "TRANSLATION_VALIDATION_FAILED"


class TranslationProvider(Protocol):
    model_name: str
    prompt_version: str

    def translate(self, prompt: str) -> dict[str, Any]: ...
