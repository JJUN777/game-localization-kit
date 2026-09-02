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
    GeminiEmptyResponseError,
    GeminiProviderBase,
    GeminiResponseError,
    structured_generation_config,
)


class GeminiImageOcrProvider(GeminiProviderBase):
    """Send one target image and its text instructions to Gemini."""

    prompt_version = OCR_PROMPT_VERSION

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        config = structured_generation_config(OCR_RESPONSE_SCHEMA)

        def request() -> dict[str, Any]:
            contents: list[types.PartUnionDict] = [prompt, image]
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            if not response.text:
                raise GeminiEmptyResponseError(
                    "Gemini returned an empty OCR response."
                )
            try:
                return validate_ocr_result(json.loads(response.text))
            except (json.JSONDecodeError, ValueError, TypeError) as error:
                raise GeminiResponseError(
                    "Gemini returned an invalid OCR response."
                ) from error

        return self.run_request(request)
