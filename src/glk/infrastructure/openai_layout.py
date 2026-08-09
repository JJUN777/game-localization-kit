"""OpenAI Responses API adapter for PDF layout reconstruction."""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from glk.extraction.layout import PROMPT_VERSION, RESPONSE_SCHEMA, build_layout_prompt
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
    image_data_url,
)


class OpenAILayoutProvider(OpenAIProviderBase):
    prompt_version = f"openai-{PROMPT_VERSION}"

    def reconstruct(
        self,
        page_number: int,
        fragments: list[dict[str, Any]],
        page_image: Image.Image,
    ) -> dict[str, Any]:
        prompt = build_layout_prompt(page_number, fragments)

        def request() -> dict[str, Any]:
            response = self.client.responses.create(
                model=self.model_name,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": image_data_url(page_image),
                                "detail": "high",
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "pdf_layout",
                        "schema": RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty layout response."
                )
            try:
                value = json.loads(response.output_text)
            except json.JSONDecodeError as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid layout response."
                ) from error
            if not isinstance(value, dict):
                raise OpenAIResponseError(
                    "OpenAI returned a non-object layout response."
                )
            return value

        return self.run_request(request)
