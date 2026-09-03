"""OpenAI adapter for constrained PDF icon auditing."""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from glk.extraction.pdf_icon_audit import (
    PDF_ICON_AUDIT_PROMPT_VERSION,
    PDF_ICON_AUDIT_RESPONSE_SCHEMA,
)
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
    image_data_url,
)


class OpenAIPdfIconAuditProvider(OpenAIProviderBase):
    """Inspect one selected PDF block crop for omitted meaningful icons."""

    prompt_version = f"openai-{PDF_ICON_AUDIT_PROMPT_VERSION}"

    def inspect(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            self.usage.begin_request()
            response = self.client.responses.create(
                model=self.model_name,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": image_data_url(image),
                                "detail": "high",
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "pdf_icon_audit",
                        "schema": PDF_ICON_AUDIT_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            self.usage.record_openai(response)
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty PDF icon audit response."
                )
            try:
                value = json.loads(response.output_text)
            except json.JSONDecodeError as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid PDF icon audit response."
                ) from error
            if not isinstance(value, dict):
                raise OpenAIResponseError(
                    "OpenAI returned a non-object PDF icon audit response."
                )
            return value

        return self.run_request(request)
