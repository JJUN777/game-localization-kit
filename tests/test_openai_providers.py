from __future__ import annotations

from types import SimpleNamespace
import unittest

import httpx
from openai import APIStatusError
from PIL import Image

from glk.infrastructure.openai_common import (
    OpenAIEmptyResponseError,
    openai_failure_code,
)
from glk.infrastructure.openai_layout import OpenAILayoutProvider
from glk.infrastructure.openai_ocr import OpenAIImageOcrProvider
from glk.infrastructure.openai_pdf_icon_audit import OpenAIPdfIconAuditProvider
from glk.infrastructure.openai_translation import OpenAITranslationProvider


class _FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.requests.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class OpenAIProviderTests(unittest.TestCase):
    def _provider(self, provider_type: type, output_text: str):
        provider = provider_type(
            api_key="sk-test",
            model_name="gpt-test",
            max_retries=1,
        )
        responses = _FakeResponses(output_text)
        provider.client = SimpleNamespace(responses=responses)
        return provider, responses

    def test_translation_uses_responses_structured_output(self) -> None:
        provider, responses = self._provider(
            OpenAITranslationProvider,
            '{"translations":[{"id":"b1","text":"번역"}]}',
        )

        value = provider.translate("Translate block b1")

        self.assertEqual(value["translations"][0]["id"], "b1")
        request = responses.requests[0]
        self.assertEqual(request["model"], "gpt-test")
        self.assertEqual(
            request["text"]["format"]["type"],  # type: ignore[index]
            "json_schema",
        )

    def test_layout_sends_a_png_data_url_with_the_prompt(self) -> None:
        provider, responses = self._provider(
            OpenAILayoutProvider,
            '{"blocks":[]}',
        )

        value = provider.reconstruct(
            1,
            [{"id": "f1", "text": "Title", "bbox": [0, 0, 10, 10]}],
            Image.new("RGB", (2, 2), "white"),
        )

        self.assertEqual(value, {"blocks": []})
        content = responses.requests[0]["input"][0]["content"]  # type: ignore[index]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

    def test_ocr_validates_the_structured_response(self) -> None:
        provider, _ = self._provider(
            OpenAIImageOcrProvider,
            '{"blocks":[],"warnings":[]}',
        )

        value = provider.transcribe(
            "Transcribe",
            Image.new("RGB", (2, 2), "white"),
        )

        self.assertEqual(value["blocks"], [])
        self.assertEqual(value["warnings"], [])
        self.assertEqual(value["status"], "needs_review")

    def test_pdf_icon_audit_uses_its_structured_schema(self) -> None:
        provider, responses = self._provider(
            OpenAIPdfIconAuditProvider,
            '{"icons":[],"summary":"No missing icons."}',
        )

        value = provider.inspect(
            "Inspect icons",
            Image.new("RGB", (2, 2), "white"),
        )

        self.assertEqual(value["icons"], [])
        request = responses.requests[0]
        self.assertEqual(
            request["text"]["format"]["name"],  # type: ignore[index]
            "pdf_icon_audit",
        )

    def test_empty_output_is_rejected(self) -> None:
        provider, _ = self._provider(OpenAITranslationProvider, "")

        with self.assertRaises(OpenAIEmptyResponseError):
            provider.translate("Translate")

    def test_api_status_is_classified_without_message_matching(self) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(429, request=request)
        error = APIStatusError("secret detail", response=response, body=None)

        self.assertEqual(openai_failure_code(error), "OPENAI_QUOTA_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
