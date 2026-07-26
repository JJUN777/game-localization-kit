from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.genai import errors

from glk.infrastructure.gemini_common import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    GeminiConfigurationError,
    GeminiResponseError,
    gemini_failure_code,
    gemini_http_options,
    gemini_retry_delay,
    is_retryable_gemini_error,
    retry_after_seconds,
    run_with_gemini_retry,
)
from glk.infrastructure.gemini_layout import GeminiLayoutProvider
from glk.infrastructure.gemini_ocr import GeminiImageOcrProvider
from glk.infrastructure.gemini_translation import GeminiTranslationProvider


def api_error(
    code: int,
    *,
    headers: dict[str, str] | None = None,
) -> errors.APIError:
    response = SimpleNamespace(headers=headers or {})
    return errors.APIError(
        code,
        {
            "error": {
                "code": code,
                "status": "TEST_STATUS",
                "message": "test failure",
            }
        },
        response,  # type: ignore[arg-type]
    )


class GeminiCommonPolicyTests(unittest.TestCase):
    def test_classifies_user_facing_failures_without_message_matching(self) -> None:
        expected = {
            400: "GEMINI_API_KEY_OR_REQUEST_INVALID",
            403: "GEMINI_PERMISSION_DENIED",
            404: "GEMINI_MODEL_NOT_FOUND",
            429: "GEMINI_QUOTA_EXCEEDED",
            503: "GEMINI_TEMPORARILY_UNAVAILABLE",
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                self.assertEqual(gemini_failure_code(api_error(status)), code)

        self.assertEqual(
            gemini_failure_code(GeminiConfigurationError("hidden detail")),
            "GEMINI_API_KEY_MISSING",
        )
        self.assertEqual(
            gemini_failure_code(GeminiResponseError("hidden detail")),
            "GEMINI_RESPONSE_INVALID",
        )
        self.assertEqual(
            gemini_failure_code(
                RuntimeError("404 model not found and 429 quota exceeded")
            ),
            "SOURCE_PROCESSING_FAILED",
        )

    def test_builds_finite_http_options_without_sdk_retries(self) -> None:
        options = gemini_http_options()

        self.assertEqual(options.timeout, DEFAULT_REQUEST_TIMEOUT_MS)
        self.assertIsNotNone(options.retry_options)
        self.assertEqual(options.retry_options.attempts, 1)

    def test_classifies_api_errors_by_status_code(self) -> None:
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                self.assertFalse(is_retryable_gemini_error(api_error(code)))
        for code in (408, 429, 500, 502, 503, 599):
            with self.subTest(code=code):
                self.assertTrue(is_retryable_gemini_error(api_error(code)))

        self.assertTrue(
            is_retryable_gemini_error(
                RuntimeError(
                    "503 transport failure with request 12404 and 400 tokens"
                )
            )
        )

    def test_honors_retry_after_and_bounds_large_values(self) -> None:
        limited = api_error(429, headers={"Retry-After": "125"})
        excessive = api_error(429, headers={"retry-after": "999"})
        invalid = api_error(429, headers={"Retry-After": "nan"})
        now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
        dated = api_error(
            503,
            headers={
                "Retry-After": format_datetime(
                    now + timedelta(seconds=90),
                    usegmt=True,
                )
            },
        )

        self.assertEqual(retry_after_seconds(limited), 125)
        self.assertEqual(retry_after_seconds(excessive), 300)
        self.assertIsNone(retry_after_seconds(invalid))
        self.assertEqual(retry_after_seconds(dated, now=now), 90)
        self.assertEqual(
            gemini_retry_delay(
                limited,
                attempt=0,
                base_delay=2,
                jitter_seconds=0.4,
            ),
            125,
        )

    def test_uses_long_fallback_for_rate_limit_without_header(self) -> None:
        delay = gemini_retry_delay(
            api_error(429),
            attempt=0,
            base_delay=2,
            jitter_seconds=0,
        )

        self.assertEqual(delay, 60)

    def test_retries_then_returns_and_does_not_retry_permanent_error(
        self,
    ) -> None:
        attempts = 0
        sleeps: list[float] = []

        def transient_operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise api_error(429, headers={"Retry-After": "7"})
            return "ok"

        result = run_with_gemini_retry(
            transient_operation,
            max_attempts=3,
            base_delay=2,
            sleep=sleeps.append,
            jitter=lambda _start, _end: 0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [7])

        permanent_attempts = 0

        def permanent_operation() -> str:
            nonlocal permanent_attempts
            permanent_attempts += 1
            raise api_error(404)

        with self.assertRaises(errors.APIError):
            run_with_gemini_retry(
                permanent_operation,
                max_attempts=3,
                base_delay=2,
                sleep=sleeps.append,
                jitter=lambda _start, _end: 0,
            )
        self.assertEqual(permanent_attempts, 1)

    def test_timeout_exception_stops_after_bounded_attempts(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise TimeoutError("request timed out")

        with self.assertRaises(TimeoutError):
            run_with_gemini_retry(
                operation,
                max_attempts=3,
                base_delay=2,
                sleep=sleeps.append,
                jitter=lambda _start, _end: 0,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [2, 4])

    def test_all_providers_apply_the_same_client_timeout(self) -> None:
        providers = (
            GeminiLayoutProvider,
            GeminiImageOcrProvider,
            GeminiTranslationProvider,
        )
        for provider_type in providers:
            with self.subTest(provider=provider_type.__name__):
                with patch(
                    "glk.infrastructure.gemini_common.genai.Client"
                ) as client:
                    provider = provider_type(
                        api_key="test-key",
                        model_name="test-model",
                        request_timeout_ms=12_345,
                    )

                options = client.call_args.kwargs["http_options"]
                self.assertEqual(provider.request_timeout_ms, 12_345)
                self.assertEqual(options.timeout, 12_345)
                self.assertEqual(options.retry_options.attempts, 1)

    def test_all_providers_use_the_shared_environment_factory(self) -> None:
        providers = (
            GeminiLayoutProvider,
            GeminiImageOcrProvider,
            GeminiTranslationProvider,
        )
        for provider_type in providers:
            with self.subTest(provider=provider_type.__name__):
                with (
                    patch(
                        "glk.infrastructure.gemini_common.load_gemini_environment",
                        return_value={
                            "GEMINI_API_KEY": "environment-key",
                            "GEMINI_MODEL": "environment-model",
                        },
                    ) as load,
                    patch(
                        "glk.infrastructure.gemini_common.genai.Client"
                    ) as client,
                ):
                    provider = provider_type.from_environment()

                load.assert_called_once_with(None)
                client.assert_called_once()
                self.assertEqual(provider.model_name, "environment-model")

    def test_provider_reads_api_key_and_model_from_explicit_settings_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings_root = root / "explicit"
            fallback_root = root / "environment"
            settings_root.mkdir()
            fallback_root.mkdir()
            (settings_root / ".env").write_text(
                'GEMINI_API_KEY="custom-root-key"\n'
                'GEMINI_MODEL="custom-root-model"\n',
                encoding="utf-8",
            )
            (fallback_root / ".env").write_text(
                'GEMINI_API_KEY="fallback-key"\n'
                'GEMINI_MODEL="fallback-model"\n',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {"GLK_SETTINGS_ROOT": str(fallback_root)},
                    clear=True,
                ),
                patch(
                    "glk.infrastructure.gemini_common.genai.Client"
                ) as client,
            ):
                provider = GeminiLayoutProvider.from_environment(
                    settings_root=settings_root,
                )
                environment_after_load = dict(os.environ)

            client.assert_called_once()
            self.assertEqual(
                client.call_args.kwargs["api_key"],
                "custom-root-key",
            )
            self.assertEqual(provider.model_name, "custom-root-model")
            self.assertEqual(
                environment_after_load,
                {"GLK_SETTINGS_ROOT": str(fallback_root)},
            )

    def test_shared_environment_factory_rejects_missing_api_key(self) -> None:
        with (
            patch(
                "glk.infrastructure.gemini_common.load_gemini_environment",
                return_value={},
            ),
        ):
            with self.assertRaisesRegex(
                GeminiConfigurationError,
                "GEMINI_API_KEY is not set",
            ):
                GeminiLayoutProvider.from_environment()


if __name__ == "__main__":
    unittest.main()
