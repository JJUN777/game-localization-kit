"""Gemini adapter for constrained PDF layout reconstruction."""

from __future__ import annotations

import json
from typing import Any

from google.genai import types
from PIL import Image

from glk.extraction.layout import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    build_layout_prompt,
)
from glk.infrastructure.gemini_common import (
    DEFAULT_MODEL,
    GeminiConfigurationError,
    GeminiProviderBase,
    load_gemini_environment,
    resolve_model_name,
)


__all__ = [
    "DEFAULT_MODEL",
    "GeminiConfigurationError",
    "GeminiLayoutProvider",
    "load_gemini_environment",
    "resolve_model_name",
]


class GeminiLayoutProvider(GeminiProviderBase):
    prompt_version = PROMPT_VERSION

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
            contents: list[types.PartUnionDict] = [prompt, page_image]
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            if not response.text:
                raise ValueError("Gemini returned an empty layout response.")
            layout = json.loads(response.text)
            if not isinstance(layout, dict):
                raise ValueError("Gemini returned a non-object layout response.")
            return layout

        return self.run_request(request)
