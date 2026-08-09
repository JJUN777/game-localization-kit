"""Shared timeout and retry policy for Gemini providers."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
from pathlib import Path
import random
import time
from typing import Callable, TypeVar

from dotenv import dotenv_values
from google import genai
from google.genai import errors, types

from glk.config import resolve_settings_root


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_REQUEST_TIMEOUT_MS = 180_000
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60.0
MAX_RETRY_DELAY_SECONDS = 300.0
_RETRYABLE_CLIENT_STATUS_CODES = frozenset({408, 429})
_GEMINI_SETTING_NAMES = ("GEMINI_API_KEY", "GEMINI_MODEL")

ResultT = TypeVar("ResultT")
ProviderT = TypeVar("ProviderT", bound="GeminiProviderBase")


class GeminiConfigurationError(ValueError):
    """Raised when Gemini credentials or model settings are unavailable."""

    code = "GEMINI_API_KEY_MISSING"


class GeminiResponseError(ValueError):
    """Raised when Gemini returns an unusable structured response."""

    code = "GEMINI_RESPONSE_INVALID"


class GeminiEmptyResponseError(GeminiResponseError):
    """Raised when Gemini returns no response text."""

    code = "GEMINI_RESPONSE_EMPTY"


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def gemini_failure_code(error: BaseException) -> str:
    """Classify provider failures without inspecting exception messages."""
    chain = _exception_chain(error)
    for item in chain:
        code = getattr(item, "code", None)
        if isinstance(code, str) and code.isupper():
            return code
    api_error = next(
        (item for item in chain if isinstance(item, errors.APIError)),
        None,
    )
    if api_error is not None:
        status = gemini_status_code(api_error)
        if status in {400, 401}:
            return "GEMINI_API_KEY_OR_REQUEST_INVALID"
        if status == 403:
            return "GEMINI_PERMISSION_DENIED"
        if status == 404:
            return "GEMINI_MODEL_NOT_FOUND"
        if status == 429:
            return "GEMINI_QUOTA_EXCEEDED"
        if status == 408 or (status is not None and 500 <= status <= 599):
            return "GEMINI_TEMPORARILY_UNAVAILABLE"
    if any(isinstance(item, (ConnectionError, TimeoutError)) for item in chain):
        return "GEMINI_NETWORK_ERROR"
    return "SOURCE_PROCESSING_FAILED"


def load_gemini_environment(
    settings_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Read effective Gemini settings without mutating process environment."""
    normalized_root = Path(settings_root) if settings_root is not None else None
    parsed = dotenv_values(resolve_settings_root(normalized_root) / ".env")
    effective: dict[str, str] = {}
    for name in _GEMINI_SETTING_NAMES:
        environment_value = os.getenv(name, "").strip()
        file_value = parsed.get(name)
        if environment_value:
            effective[name] = environment_value
        elif isinstance(file_value, str) and file_value.strip():
            effective[name] = file_value.strip()
    return effective


def _configured_model_name(
    model_name: str | None,
    environment: dict[str, str],
) -> str:
    if model_name and model_name.strip():
        return model_name.strip()
    environment_model = environment.get("GEMINI_MODEL", "").strip()
    if environment_model:
        return environment_model
    return DEFAULT_MODEL


def resolve_model_name(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve an explicit, configured, or default Gemini model name."""
    environment = load_gemini_environment(settings_root)
    return _configured_model_name(model_name, environment)


def gemini_http_options(
    timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
) -> types.HttpOptions:
    """Build finite request options with SDK retries disabled."""
    if (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or timeout_ms < 1
    ):
        raise ValueError("Gemini request timeout must be a positive integer.")
    return types.HttpOptions(
        timeout=timeout_ms,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


def gemini_status_code(error: BaseException) -> int | None:
    """Return the stable SDK status code when this is a Gemini API error."""
    if not isinstance(error, errors.APIError):
        return None
    code = error.code
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def is_retryable_gemini_error(error: BaseException) -> bool:
    """Classify API failures by status code instead of message text."""
    code = gemini_status_code(error)
    if code is None:
        # Empty/invalid model responses and transport exceptions were retryable
        # before this shared policy and remain so.
        return True
    if code in _RETRYABLE_CLIENT_STATUS_CODES:
        return True
    if 500 <= code <= 599:
        return True
    if 400 <= code <= 499:
        return False
    return True


def _retry_after_header(error: BaseException) -> str | None:
    if not isinstance(error, errors.APIError):
        return None
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    for name in ("Retry-After", "retry-after"):
        try:
            value = headers.get(name)
        except (AttributeError, TypeError):
            return None
        if value is not None:
            return str(value).strip() or None
    return None


def retry_after_seconds(
    error: BaseException,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse Retry-After seconds or an HTTP date from an SDK response."""
    value = _retry_after_header(error)
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        seconds = (retry_at - current).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_DELAY_SECONDS)


def gemini_retry_delay(
    error: BaseException,
    *,
    attempt: int,
    base_delay: float,
    jitter_seconds: float,
) -> float:
    """Return a bounded delay, honoring Retry-After when available."""
    retry_after = retry_after_seconds(error)
    if retry_after is not None:
        return retry_after
    exponential = base_delay * (2**attempt)
    if gemini_status_code(error) == 429:
        exponential = max(
            exponential,
            DEFAULT_RATE_LIMIT_DELAY_SECONDS,
        )
    return min(
        max(exponential, 0.0) + max(jitter_seconds, 0.0),
        MAX_RETRY_DELAY_SECONDS,
    )


def run_with_gemini_retry(
    operation: Callable[[], ResultT],
    *,
    max_attempts: int,
    base_delay: float,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
) -> ResultT:
    """Run one provider operation with the shared bounded retry policy."""
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ValueError("Gemini max attempts must be a positive integer.")
    if base_delay < 0:
        raise ValueError("Gemini retry base delay must not be negative.")
    sleep_for = sleep or time.sleep
    random_jitter = jitter or random.uniform
    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as error:
            if (
                attempt == max_attempts - 1
                or not is_retryable_gemini_error(error)
            ):
                raise
            delay = gemini_retry_delay(
                error,
                attempt=attempt,
                base_delay=base_delay,
                jitter_seconds=random_jitter(0.0, 0.5),
            )
            sleep_for(delay)
    raise RuntimeError("Gemini retry loop ended unexpectedly.")


class GeminiProviderBase:
    """Shared configuration, client creation, and retry shell for providers."""

    provider_name = "gemini"

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
        cls: type[ProviderT],
        model_name: str | None = None,
        *,
        settings_root: str | os.PathLike[str] | None = None,
    ) -> ProviderT:
        """Create one provider from the shared settings location."""
        environment = load_gemini_environment(settings_root)
        api_key = environment.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is not set. Add it to .env or export it in the shell."
            )
        return cls(
            api_key=api_key,
            model_name=_configured_model_name(model_name, environment),
        )

    def run_request(self, operation: Callable[[], ResultT]) -> ResultT:
        """Run one provider request with the shared retry policy."""
        return run_with_gemini_retry(
            operation,
            max_attempts=self.max_retries,
            base_delay=self.base_delay,
        )
