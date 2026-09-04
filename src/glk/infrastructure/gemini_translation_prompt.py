"""Gemini adapter for project translation prompt drafts."""

from __future__ import annotations

import json
from typing import Any

from glk.extraction.translation_prompt_draft import (
    TRANSLATION_PROMPT_DRAFT_RESPONSE_SCHEMA,
    TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
    TRANSLATION_PROMPT_DRAFT_VERSION,
)
from glk.infrastructure.gemini_common import (
    GeminiEmptyResponseError,
    GeminiProviderBase,
    GeminiResponseError,
    structured_generation_config,
)


class GeminiTranslationPromptDraftProvider(GeminiProviderBase):
    """Generate an editable style prompt from representative source text."""

    prompt_version = TRANSLATION_PROMPT_DRAFT_VERSION

    def generate_draft(self, prompt: str) -> dict[str, Any]:
        config = structured_generation_config(
            TRANSLATION_PROMPT_DRAFT_RESPONSE_SCHEMA,
            system_instruction=TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )

        def request() -> dict[str, Any]:
            self.usage.begin_request()
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            self.usage.record_gemini(response)
            if not response.text:
                raise GeminiEmptyResponseError(
                    "Gemini returned an empty translation prompt draft."
                )
            try:
                value = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise GeminiResponseError(
                    "Gemini returned an invalid translation prompt draft."
                ) from error
            if not isinstance(value, dict):
                raise GeminiResponseError(
                    "Gemini returned a non-object translation prompt draft."
                )
            return value

        return self.run_request(request)
