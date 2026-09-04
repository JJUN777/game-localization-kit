"""OpenAI adapter for project translation prompt drafts."""

from __future__ import annotations

import json
from typing import Any

from glk.extraction.translation_prompt_draft import (
    TRANSLATION_PROMPT_DRAFT_RESPONSE_SCHEMA,
    TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
    TRANSLATION_PROMPT_DRAFT_VERSION,
)
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
)


class OpenAITranslationPromptDraftProvider(OpenAIProviderBase):
    """Generate an editable style prompt from representative source text."""

    prompt_version = f"openai-{TRANSLATION_PROMPT_DRAFT_VERSION}"

    def generate_draft(self, prompt: str) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            self.usage.begin_request()
            response = self.client.responses.create(
                model=self.model_name,
                instructions=TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "translation_prompt_draft",
                        "schema": TRANSLATION_PROMPT_DRAFT_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            self.usage.record_openai(response)
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty translation prompt draft."
                )
            try:
                value = json.loads(response.output_text)
            except json.JSONDecodeError as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid translation prompt draft."
                ) from error
            if not isinstance(value, dict):
                raise OpenAIResponseError(
                    "OpenAI returned a non-object translation prompt draft."
                )
            return value

        return self.run_request(request)
