"""Gemini adapter for constrained PDF icon auditing."""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from glk.extraction.pdf_icon_audit import (
    PDF_ICON_AUDIT_PROMPT_VERSION,
    PDF_ICON_AUDIT_RESPONSE_SCHEMA,
)
from glk.infrastructure.gemini_common import (
    GeminiEmptyResponseError,
    GeminiProviderBase,
    GeminiResponseError,
    structured_generation_config,
)


class GeminiPdfIconAuditProvider(GeminiProviderBase):
    """Inspect one selected PDF block crop for omitted meaningful icons."""

    prompt_version = PDF_ICON_AUDIT_PROMPT_VERSION

    def inspect(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        config = structured_generation_config(PDF_ICON_AUDIT_RESPONSE_SCHEMA)

        def request() -> dict[str, Any]:
            self.usage.begin_request()
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[prompt, image],
                config=config,
            )
            self.usage.record_gemini(response)
            if not response.text:
                raise GeminiEmptyResponseError(
                    "Gemini returned an empty PDF icon audit response."
                )
            try:
                value = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise GeminiResponseError(
                    "Gemini returned an invalid PDF icon audit response."
                ) from error
            if not isinstance(value, dict):
                raise GeminiResponseError(
                    "Gemini returned a non-object PDF icon audit response."
                )
            return value

        return self.run_request(request)
