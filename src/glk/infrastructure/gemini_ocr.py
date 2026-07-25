"""Gemini adapter for structured image OCR."""

from __future__ import annotations

import json
from typing import Any

from google import genai
from google.genai import types
from PIL import Image

from glk.extraction.image_ocr import (
    OCR_PROMPT_VERSION,
    OCR_RESPONSE_SCHEMA,
    validate_ocr_result,
)
from glk.infrastructure.gemini_common import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    gemini_http_options,
    run_with_gemini_retry,
)
from glk.infrastructure.gemini_layout import (
    GeminiConfigurationError,
    load_gemini_environment,
    resolve_model_name,
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
        request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
    ) -> None:
        if not api_key.strip():
            raise GeminiConfigurationError("GEMINI_API_KEY is not configured.")
        self.model_name = model_name
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.request_timeout_ms = request_timeout_ms
        self.client = genai.Client(
            api_key=api_key,
            http_options=gemini_http_options(request_timeout_ms),
        )

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

        def request() -> dict[str, Any]:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[prompt, image],
                config=config,
            )
            if not response.text:
                raise ValueError("Gemini returned an empty OCR response.")
            return validate_ocr_result(json.loads(response.text))

        return run_with_gemini_retry(
            request,
            max_attempts=self.max_retries,
            base_delay=self.base_delay,
        )
