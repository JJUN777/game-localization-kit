"""Gemini adapter for constrained PDF layout reconstruction."""

from __future__ import annotations

import json
import os
from pathlib import Path
import random
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

from glk.extraction.layout import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    build_layout_prompt,
)


DEFAULT_MODEL = "gemini-2.5-flash"
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


class GeminiConfigurationError(ValueError):
    """Raised when Gemini credentials or model settings are unavailable."""


def load_gemini_environment() -> None:
    """Load .env from the working directory or editable source root."""
    working_env = Path.cwd() / ".env"
    load_dotenv(working_env, override=False)
    source_env = Path(__file__).resolve().parents[3] / ".env"
    if source_env != working_env:
        load_dotenv(source_env, override=False)


def resolve_model_name(model_name: str | None = None) -> str:
    load_gemini_environment()
    if model_name and model_name.strip():
        return model_name.strip()
    environment_model = os.getenv("GEMINI_MODEL", "").strip()
    if environment_model:
        return environment_model
    return DEFAULT_MODEL


def _is_retryable_error(error: Exception) -> bool:
    message = str(error).lower()
    if any(str(code) in message for code in _NON_RETRYABLE_STATUS_CODES):
        return False
    permanent_markers = (
        "invalid api key",
        "permission denied",
        "not found",
        "invalid argument",
    )
    return not any(marker in message for marker in permanent_markers)


class GeminiLayoutProvider:
    prompt_version = PROMPT_VERSION

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
        for attempt in range(self.max_retries):
            try:
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
            except Exception as error:
                if attempt == self.max_retries - 1 or not _is_retryable_error(error):
                    raise
                delay = self.base_delay * (2**attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
        raise RuntimeError("Gemini retry loop ended unexpectedly.")
