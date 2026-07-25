"""Gemini adapter for constrained PDF layout reconstruction."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

from glk.config import resolve_settings_root
from glk.extraction.layout import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    build_layout_prompt,
)
from glk.infrastructure.gemini_common import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    gemini_http_options,
    run_with_gemini_retry,
)


DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiConfigurationError(ValueError):
    """Raised when Gemini credentials or model settings are unavailable."""


def load_gemini_environment(
    settings_root: str | os.PathLike[str] | None = None,
) -> None:
    """Load Gemini settings from the same stable root used by the dashboard."""
    load_dotenv(
        resolve_settings_root(settings_root) / ".env",
        override=False,
    )


def resolve_model_name(model_name: str | None = None) -> str:
    load_gemini_environment()
    if model_name and model_name.strip():
        return model_name.strip()
    environment_model = os.getenv("GEMINI_MODEL", "").strip()
    if environment_model:
        return environment_model
    return DEFAULT_MODEL


class GeminiLayoutProvider:
    prompt_version = PROMPT_VERSION

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
    def from_environment(cls, model_name: str | None = None) -> GeminiLayoutProvider:
        load_gemini_environment()
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is not set. Add it to .env or export it in the shell."
            )
        return cls(api_key=api_key, model_name=resolve_model_name(model_name))

    def reconstruct(
        self, page_number: int, fragments: list[dict[str, Any]], page_image: Image.Image
    ) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
        )
        prompt = build_layout_prompt(page_number, fragments)

        def request() -> dict[str, Any]:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[prompt, page_image],
                config=config,
            )
            if not response.text:
                raise ValueError("Gemini returned an empty layout response.")
            layout = json.loads(response.text)
            if not isinstance(layout, dict):
                raise ValueError("Gemini returned a non-object layout response.")
            return layout

        return run_with_gemini_retry(
            request,
            max_attempts=self.max_retries,
            base_delay=self.base_delay,
        )
