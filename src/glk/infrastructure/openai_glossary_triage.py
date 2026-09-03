"""OpenAI adapter for structured glossary candidate triage."""

from __future__ import annotations

import json
from typing import Any

from glk.extraction.glossary_triage import (
    GLOSSARY_TRIAGE_PROMPT_VERSION,
    GLOSSARY_TRIAGE_RESPONSE_SCHEMA,
    GLOSSARY_TRIAGE_SYSTEM_INSTRUCTION,
)
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
)


class OpenAIGlossaryTriageProvider(OpenAIProviderBase):
    """Classify one bounded chunk of local glossary candidates."""

    prompt_version = f"openai-{GLOSSARY_TRIAGE_PROMPT_VERSION}"

    def triage(self, prompt: str) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            self.usage.begin_request()
            response = self.client.responses.create(
                model=self.model_name,
                instructions=GLOSSARY_TRIAGE_SYSTEM_INSTRUCTION,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "glossary_candidate_triage",
                        "schema": GLOSSARY_TRIAGE_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            self.usage.record_openai(response)
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty glossary triage response."
                )
            try:
                value = json.loads(response.output_text)
            except json.JSONDecodeError as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid glossary triage response."
                ) from error
            if not isinstance(value, dict):
                raise OpenAIResponseError(
                    "OpenAI returned a non-object glossary triage response."
                )
            return value

        return self.run_request(request)
