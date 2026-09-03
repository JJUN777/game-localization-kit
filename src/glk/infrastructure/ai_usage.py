"""Normalize provider usage metadata and estimate standard API cost."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


_PRICES_USD_PER_MILLION: dict[str, tuple[float, float, float | None]] = {
    # Gemini standard paid tier. Gemini 3.7/3.6 introductory rates expire
    # after 2026-12-31; keep this table date-stamped in docs/COSTS.md.
    "gemini-3.7-flash": (0.75, 3.75, 0.075),
    "gemini-3.6-flash": (0.75, 3.75, 0.075),
    "gemini-3.5-flash": (1.50, 9.00, 0.15),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
    "gemini-2.5-pro": (1.25, 10.00, 0.125),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
    # OpenAI standard Responses API rates shown in the model comparison.
    "gpt-5.6-sol": (4.00, 20.00, 0.40),
    "gpt-5.6-terra": (2.00, 12.00, 0.20),
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
}


def _value(source: Any, name: str, default: int = 0) -> int:
    raw = source.get(name, default) if isinstance(source, dict) else getattr(
        source, name, default
    )
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


@dataclass(slots=True)
class AiUsageAccumulator:
    provider: str
    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_input_tokens: int = 0

    def begin_request(self) -> None:
        self.requests += 1

    def record_gemini(self, response: Any) -> None:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            return
        thinking = _value(metadata, "thoughts_token_count")
        self.input_tokens += _value(metadata, "prompt_token_count")
        self.output_tokens += _value(metadata, "candidates_token_count") + thinking
        self.thinking_tokens += thinking
        self.cached_input_tokens += _value(
            metadata,
            "cached_content_token_count",
        )

    def record_openai(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        self.input_tokens += _value(usage, "input_tokens")
        self.output_tokens += _value(usage, "output_tokens")
        self.cached_input_tokens += _value(input_details, "cached_tokens")
        self.thinking_tokens += _value(output_details, "reasoning_tokens")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = asdict(self)
        value["total_tokens"] = self.input_tokens + self.output_tokens
        prices = _PRICES_USD_PER_MILLION.get(self.model)
        if prices is None:
            value["estimated_cost_usd"] = None
            value["pricing_available"] = False
            return value
        input_price, output_price, cached_price = prices
        cached = min(self.cached_input_tokens, self.input_tokens)
        uncached = self.input_tokens - cached
        effective_cached_price = input_price if cached_price is None else cached_price
        estimate = (
            uncached * input_price
            + cached * effective_cached_price
            + self.output_tokens * output_price
        ) / 1_000_000
        value["estimated_cost_usd"] = round(estimate, 8)
        value["pricing_available"] = True
        return value


def provider_usage(provider: Any) -> dict[str, Any] | None:
    """Return a serializable usage snapshot when the provider supports it."""
    usage = getattr(provider, "usage", None)
    if not isinstance(usage, AiUsageAccumulator):
        return None
    return usage.to_dict()
