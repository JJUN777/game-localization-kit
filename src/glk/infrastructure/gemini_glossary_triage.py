"""Gemini adapter for structured glossary candidate triage."""

from __future__ import annotations

import json
from typing import Any

from glk.extraction.glossary_triage import (
    GLOSSARY_TRIAGE_PROMPT_VERSION,
    GLOSSARY_TRIAGE_RESPONSE_SCHEMA,
    GLOSSARY_TRIAGE_SYSTEM_INSTRUCTION,
)
from glk.infrastructure.gemini_common import (
    GeminiEmptyResponseError,
    GeminiProviderBase,
    GeminiResponseError,
    structured_generation_config,
)


class GeminiGlossaryTriageProvider(GeminiProviderBase):
    """Classify one bounded chunk of local glossary candidates."""

    prompt_version = GLOSSARY_TRIAGE_PROMPT_VERSION

    def triage(self, prompt: str) -> dict[str, Any]:
        config = structured_generation_config(
            GLOSSARY_TRIAGE_RESPONSE_SCHEMA,
            system_instruction=GLOSSARY_TRIAGE_SYSTEM_INSTRUCTION,
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
                    "Gemini returned an empty glossary triage response."
                )
            try:
                value = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise GeminiResponseError(
                    "Gemini returned an invalid glossary triage response."
                ) from error
            if not isinstance(value, dict):
                raise GeminiResponseError(
                    "Gemini returned a non-object glossary triage response."
                )
            return value

        return self.run_request(request)
