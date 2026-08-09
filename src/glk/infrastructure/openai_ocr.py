"""OpenAI Responses API adapter for structured image OCR."""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from glk.extraction.image_ocr import (
    OCR_PROMPT_VERSION,
    OCR_RESPONSE_SCHEMA,
    validate_ocr_result,
)
from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    OpenAIProviderBase,
    OpenAIResponseError,
    image_data_url,
)


class OpenAIImageOcrProvider(OpenAIProviderBase):
    prompt_version = f"openai-{OCR_PROMPT_VERSION}"

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]:
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
                                "image_url": image_data_url(image),
                                "detail": "high",
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "image_ocr",
                        "schema": OCR_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )
            if not response.output_text:
                raise OpenAIEmptyResponseError(
                    "OpenAI returned an empty OCR response."
                )
            try:
                return validate_ocr_result(json.loads(response.output_text))
            except (json.JSONDecodeError, ValueError, TypeError) as error:
                raise OpenAIResponseError(
                    "OpenAI returned an invalid OCR response."
                ) from error

        return self.run_request(request)
