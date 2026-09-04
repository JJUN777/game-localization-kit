from types import SimpleNamespace
import unittest

from glk.infrastructure.ai_usage import (
    AiUsageAccumulator,
    estimate_ai_cost,
    usage_delta,
)


class AiUsageAccumulatorTests(unittest.TestCase):
    def test_estimates_uncached_cost_and_returns_none_for_unknown_model(self) -> None:
        self.assertAlmostEqual(
            estimate_ai_cost(
                "gemini-3.8-flash",
                input_tokens=1_000,
                output_tokens=400,
            ),
            0.00225,
        )
        self.assertIsNone(
            estimate_ai_cost(
                "unknown-model",
                input_tokens=1_000,
                output_tokens=400,
            )
        )

    def test_calculates_usage_delta(self) -> None:
        result = usage_delta(
            {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "requests": 2,
                "input_tokens": 100,
                "output_tokens": 40,
                "thinking_tokens": 5,
                "cached_input_tokens": 10,
                "total_tokens": 140,
                "estimated_cost_usd": 0.1,
                "pricing_available": True,
            },
            {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "requests": 3,
                "input_tokens": 250,
                "output_tokens": 90,
                "thinking_tokens": 8,
                "cached_input_tokens": 15,
                "total_tokens": 340,
                "estimated_cost_usd": 0.25,
                "pricing_available": True,
            },
        )

        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["input_tokens"], 150)
        self.assertEqual(result["output_tokens"], 50)
        self.assertEqual(result["total_tokens"], 200)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.15)

    def test_accumulates_gemini_usage_and_estimates_cost(self) -> None:
        usage = AiUsageAccumulator("gemini", "gemini-3.8-flash")

        usage.begin_request()
        usage.record_gemini(
            SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=1_000,
                    candidates_token_count=400,
                    thoughts_token_count=100,
                    cached_content_token_count=200,
                )
            )
        )

        result = usage.to_dict()
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["input_tokens"], 1_000)
        self.assertEqual(result["output_tokens"], 500)
        self.assertEqual(result["thinking_tokens"], 100)
        self.assertEqual(result["total_tokens"], 1_500)
        self.assertTrue(result["pricing_available"])
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.00249)

    def test_accumulates_openai_cached_and_reasoning_tokens(self) -> None:
        usage = AiUsageAccumulator("openai", "gpt-5.6-terra")

        usage.begin_request()
        usage.record_openai(
            SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=2_000,
                    output_tokens=500,
                    input_tokens_details=SimpleNamespace(cached_tokens=500),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=120),
                )
            )
        )

        result = usage.to_dict()
        self.assertEqual(result["cached_input_tokens"], 500)
        self.assertEqual(result["thinking_tokens"], 120)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.0091)

    def test_keeps_usage_when_model_price_is_unknown(self) -> None:
        usage = AiUsageAccumulator("gemini", "gemini-custom")
        usage.begin_request()
        usage.record_gemini(
            SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=5,
                )
            )
        )

        result = usage.to_dict()
        self.assertEqual(result["total_tokens"], 15)
        self.assertFalse(result["pricing_available"])
        self.assertIsNone(result["estimated_cost_usd"])


if __name__ == "__main__":
    unittest.main()
