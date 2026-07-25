"""Shared timeout and retry policy for Gemini providers."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
import time
from typing import Callable, TypeVar

from google.genai import errors, types


DEFAULT_REQUEST_TIMEOUT_MS = 180_000
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60.0
MAX_RETRY_DELAY_SECONDS = 300.0
_RETRYABLE_CLIENT_STATUS_CODES = frozenset({408, 429})

ResultT = TypeVar("ResultT")


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
