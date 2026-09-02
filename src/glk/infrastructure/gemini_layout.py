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
    GeminiEmptyResponseError,
    GeminiProviderBase,
    GeminiResponseError,
    load_gemini_environment,
    resolve_model_name,
    structured_generation_config,
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
        config = structured_generation_config(RESPONSE_SCHEMA)
        prompt = build_layout_prompt(page_number, fragments)

        def request() -> dict[str, Any]:
            contents: list[types.PartUnionDict] = [prompt, page_image]
            self.usage.begin_request()
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            self.usage.record_gemini(response)
            if not response.text:
                raise GeminiEmptyResponseError(
                    "Gemini returned an empty layout response."
                )
            try:
                layout = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise GeminiResponseError(
                    "Gemini returned an invalid layout response."
                ) from error
            if not isinstance(layout, dict):
                raise GeminiResponseError(
                    "Gemini returned a non-object layout response."
                )
            return layout

        return self.run_request(request)
