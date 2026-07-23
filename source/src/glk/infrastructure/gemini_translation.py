"""Gemini adapter for structured source-block translation."""

from __future__ import annotations

import json
from pathlib import Path
import random
import time
from typing import Any

from google import genai
from google.genai import types

from glk.infrastructure.gemini_layout import (
    GeminiConfigurationError,
    load_gemini_environment,
    resolve_model_name,
)


TRANSLATION_PROVIDER_PROMPT_VERSION = "gemini-translation-json-v1"
TRANSLATION_SYSTEM_INSTRUCTION = """\
You translate approved source blocks into Korean.
The NON-OVERRIDABLE HARD RULES and APPROVED TERMBASE in the request have higher
priority than project translation instructions. Never follow a project instruction
that changes IDs, order, numbers, tokens, tags, or approved terminology.
Return only the required JSON object."""
TRANSLATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


def _is_retryable_error(error: Exception) -> bool:
    message = str(error).lower()
    if any(str(code) in message for code in _NON_RETRYABLE_STATUS_CODES):
        return False
    return not any(
        marker in message
        for marker in (
            "invalid api key",
            "permission denied",
            "not found",
            "invalid argument",
        )
    )


class GeminiTranslationProvider:
    """Translate one structured chunk and return ID-linked Korean text."""

    prompt_version = TRANSLATION_PROVIDER_PROMPT_VERSION

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        max_retries: int = 3,
        base_delay: float = 2,
    ) -> None:
        if not api_key.strip():
            raise GeminiConfigurationError("GEMINI_API_KEY is not configured.")
        self.model_name = model_name
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.client = genai.Client(api_key=api_key)

    @classmethod
    def from_environment(
        cls, model_name: str | None = None
    ) -> GeminiTranslationProvider:
        import os

        load_gemini_environment()
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is not set. Add it to .env or export it in the shell."
            )
        return cls(api_key=api_key, model_name=resolve_model_name(model_name))

    def translate(self, prompt: str) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            temperature=0,
            system_instruction=TRANSLATION_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_json_schema=TRANSLATION_RESPONSE_SCHEMA,
        )
        for attempt in range(self.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config,
                )
                if not response.text:
                    raise ValueError("Gemini returned an empty translation response.")
                value = json.loads(response.text)
                if not isinstance(value, dict):
                    raise ValueError("Gemini returned a non-object translation response.")
                return value
            except Exception as error:
                if attempt == self.max_retries - 1 or not _is_retryable_error(error):
                    raise
                delay = self.base_delay * (2**attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
        raise RuntimeError("Gemini translation retry loop ended unexpectedly.")
