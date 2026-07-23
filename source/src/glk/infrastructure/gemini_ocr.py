"""Gemini adapter for structured image OCR."""

from __future__ import annotations

import json
from pathlib import Path
import random
import time
from typing import Any

from google import genai
from google.genai import types
from PIL import Image

from glk.extraction.image_ocr import (
    OCR_PROMPT_VERSION,
    OCR_RESPONSE_SCHEMA,
    validate_ocr_result,
)
from glk.infrastructure.gemini_layout import (
    GeminiConfigurationError,
    load_gemini_environment,
    resolve_model_name,
)


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


class GeminiImageOcrProvider:
    """Send one target image and its text instructions to Gemini."""

    prompt_version = OCR_PROMPT_VERSION

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
    ) -> GeminiImageOcrProvider:
        import os

        load_gemini_environment()
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is not set. Add it to .env or export it in the shell."
            )
        return cls(api_key=api_key, model_name=resolve_model_name(model_name))

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=OCR_RESPONSE_SCHEMA,
        )
        for attempt in range(self.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=[prompt, image],
                    config=config,
                )
                if not response.text:
                    raise ValueError("Gemini returned an empty OCR response.")
                return validate_ocr_result(json.loads(response.text))
            except Exception as error:
                if attempt == self.max_retries - 1 or not _is_retryable_error(error):
                    raise
                delay = self.base_delay * (2**attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
        raise RuntimeError("Gemini OCR retry loop ended unexpectedly.")
