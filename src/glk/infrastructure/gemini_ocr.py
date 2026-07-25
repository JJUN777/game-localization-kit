"""Gemini adapter for structured image OCR."""

from __future__ import annotations

import json
from typing import Any

from google.genai import types
from PIL import Image

from glk.extraction.image_ocr import (
    OCR_PROMPT_VERSION,
    OCR_RESPONSE_SCHEMA,
    validate_ocr_result,
)
from glk.infrastructure.gemini_common import (
    GeminiProviderBase,
)


class GeminiImageOcrProvider(GeminiProviderBase):
    """Send one target image and its text instructions to Gemini."""

    prompt_version = OCR_PROMPT_VERSION

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=OCR_RESPONSE_SCHEMA,
        )

        def request() -> dict[str, Any]:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[prompt, image],
                config=config,
            )
            if not response.text:
                raise ValueError("Gemini returned an empty OCR response.")
            return validate_ocr_result(json.loads(response.text))

        return self.run_request(request)
