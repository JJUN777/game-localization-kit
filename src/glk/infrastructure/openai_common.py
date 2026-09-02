"""Shared configuration and retry policy for OpenAI providers."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
import base64
import math
import os
from pathlib import Path
import random
import time
from typing import Callable, TypeVar

from dotenv import dotenv_values
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from PIL import Image

from glk.config import resolve_settings_root
from glk.infrastructure.ai_usage import AiUsageAccumulator


DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60.0
MAX_RETRY_DELAY_SECONDS = 300.0
_OPENAI_SETTING_NAMES = ("OPENAI_API_KEY", "OPENAI_MODEL")
_RETRYABLE_CLIENT_STATUS_CODES = frozenset({408, 409, 429})

ResultT = TypeVar("ResultT")
ProviderT = TypeVar("ProviderT", bound="OpenAIProviderBase")


class OpenAIConfigurationError(ValueError):
    """Raised when OpenAI credentials are unavailable."""

    code = "OPENAI_API_KEY_MISSING"


class OpenAIResponseError(ValueError):
    """Raised when OpenAI returns an unusable structured response."""

    code = "OPENAI_RESPONSE_INVALID"


class OpenAIEmptyResponseError(OpenAIResponseError):
    """Raised when OpenAI returns no output text."""

    code = "OPENAI_RESPONSE_EMPTY"


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def openai_status_code(error: BaseException) -> int | None:
    """Return the stable HTTP status code for an OpenAI SDK error."""
    if not isinstance(error, APIStatusError):
        return None
    status = error.status_code
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def openai_failure_code(error: BaseException) -> str:
    """Classify OpenAI failures without exposing exception messages."""
    chain = _exception_chain(error)
    for item in chain:
        code = getattr(item, "code", None)
        if isinstance(code, str) and code.isupper():
            return code
    api_error = next(
        (item for item in chain if isinstance(item, APIStatusError)),
        None,
    )
    if api_error is not None:
        status = openai_status_code(api_error)
        if status in {400, 401}:
            return "OPENAI_API_KEY_OR_REQUEST_INVALID"
        if status == 403:
            return "OPENAI_PERMISSION_DENIED"
        if status == 404:
            return "OPENAI_MODEL_NOT_FOUND"
        if status == 429:
            return "OPENAI_QUOTA_EXCEEDED"
        if status == 408 or (status is not None and 500 <= status <= 599):
            return "OPENAI_TEMPORARILY_UNAVAILABLE"
    if any(
        isinstance(item, (APIConnectionError, APITimeoutError))
        for item in chain
    ):
        return "OPENAI_NETWORK_ERROR"
    return "SOURCE_PROCESSING_FAILED"


def load_openai_environment(
    settings_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Read effective OpenAI settings without mutating process environment."""
    normalized_root = Path(settings_root) if settings_root is not None else None
    parsed = dotenv_values(resolve_settings_root(normalized_root) / ".env")
    effective: dict[str, str] = {}
    for name in _OPENAI_SETTING_NAMES:
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
    configured = environment.get("OPENAI_MODEL", "").strip()
    return configured or DEFAULT_OPENAI_MODEL


def resolve_openai_model_name(
    model_name: str | None = None,
    *,
    settings_root: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve an explicit, configured, or default OpenAI model name."""
    return _configured_model_name(
        model_name,
        load_openai_environment(settings_root),
    )


def is_retryable_openai_error(error: BaseException) -> bool:
    """Classify retryable OpenAI failures using SDK types and status codes."""
    if isinstance(error, (APIConnectionError, APITimeoutError)):
        return True
    status = openai_status_code(error)
    if status is None:
        return True
    if status in _RETRYABLE_CLIENT_STATUS_CODES or 500 <= status <= 599:
        return True
    return not 400 <= status <= 499


def _retry_after_seconds(
    error: BaseException,
    *,
    now: datetime | None = None,
) -> float | None:
    if not isinstance(error, APIStatusError):
        return None
    headers = getattr(error.response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
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
        seconds = (retry_at - (now or datetime.now(timezone.utc))).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_DELAY_SECONDS)


def openai_retry_delay(
    error: BaseException,
    *,
    attempt: int,
    base_delay: float,
    jitter_seconds: float,
) -> float:
    """Return a bounded exponential delay, honoring Retry-After."""
    retry_after = _retry_after_seconds(error)
    if retry_after is not None:
        return retry_after
    exponential = base_delay * (2**attempt)
    if openai_status_code(error) == 429:
        exponential = max(exponential, DEFAULT_RATE_LIMIT_DELAY_SECONDS)
    return min(
        max(exponential, 0.0) + max(jitter_seconds, 0.0),
        MAX_RETRY_DELAY_SECONDS,
    )


def run_with_openai_retry(
    operation: Callable[[], ResultT],
    *,
    max_attempts: int,
    base_delay: float,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
) -> ResultT:
    """Run one OpenAI request with a bounded retry policy."""
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ValueError("OpenAI max attempts must be a positive integer.")
    if base_delay < 0:
        raise ValueError("OpenAI retry base delay must not be negative.")
    sleep_for = sleep or time.sleep
    random_jitter = jitter or random.uniform
    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as error:
            if (
                attempt == max_attempts - 1
                or not is_retryable_openai_error(error)
            ):
                raise
            sleep_for(
                openai_retry_delay(
                    error,
                    attempt=attempt,
                    base_delay=base_delay,
                    jitter_seconds=random_jitter(0.0, 0.5),
                )
            )
    raise RuntimeError("OpenAI retry loop ended unexpectedly.")


def image_data_url(image: Image.Image) -> str:
    """Encode a PIL image as a PNG data URL accepted by the Responses API."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class OpenAIProviderBase:
    """Shared credential loading, client creation, and retry shell."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        max_retries: int = 3,
        base_delay: float = 2,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise OpenAIConfigurationError("OPENAI_API_KEY is not configured.")
        self.model_name = model_name
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.usage = AiUsageAccumulator(self.provider_name, model_name)
        self.client = OpenAI(
            api_key=api_key,
            timeout=request_timeout_seconds,
            max_retries=0,
        )

    @classmethod
    def from_environment(
        cls: type[ProviderT],
        model_name: str | None = None,
        *,
        settings_root: str | os.PathLike[str] | None = None,
    ) -> ProviderT:
        environment = load_openai_environment(settings_root)
        api_key = environment.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise OpenAIConfigurationError(
                "OPENAI_API_KEY is not set. Add it to .env or export it in the shell."
            )
        return cls(
            api_key=api_key,
            model_name=_configured_model_name(model_name, environment),
        )

    def run_request(self, operation: Callable[[], ResultT]) -> ResultT:
        try:
            return run_with_openai_retry(
                operation,
                max_attempts=self.max_retries,
                base_delay=self.base_delay,
            )
        except Exception as error:
            error.ai_usage = self.usage.to_dict()  # type: ignore[attr-defined]
            raise
