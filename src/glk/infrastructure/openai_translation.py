"""OpenAI Responses API adapter for structured source-block translation."""

from __future__ import annotations

import json
from typing import Any

from glk.infrastructure.gemini_translation import (
    TRANSLATION_RESPONSE_SCHEMA,
    TRANSLATION_SYSTEM_INSTRUCTION,
)
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
)


TRANSLATION_PROVIDER_PROMPT_VERSION = "openai-translation-json-v1"


class OpenAITranslationProvider(OpenAIProviderBase):
    prompt_version = TRANSLATION_PROVIDER_PROMPT_VERSION

    def translate(self, prompt: str) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            response = self.client.responses.create(
                model=self.model_name,
                instructions=TRANSLATION_SYSTEM_INSTRUCTION,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "translations",
                        "schema": TRANSLATION_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty translation response."
                )
            try:
                value = json.loads(response.output_text)
            except json.JSONDecodeError as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid translation response."
                ) from error
            if not isinstance(value, dict):
                raise OpenAIResponseError(
                    "OpenAI returned a non-object translation response."
                )
            return value

        return self.run_request(request)
