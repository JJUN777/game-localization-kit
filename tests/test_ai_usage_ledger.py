from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from glk.application.ai_usage_ledger import (
    append_ai_usage_event,
    summarize_project_ai_usage,
)


class AiUsageLedgerTests(unittest.TestCase):
    def test_aggregates_stages_and_tracks_unpriced_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_path = Path(temporary) / "project"
            append_ai_usage_event(
                project_path,
                stage="icon_audit",
                operation="pdf_block_inspection",
                usage={
                    "provider": "gemini",
                    "model": "priced-model",
                    "requests": 2,
                    "input_tokens": 1_200,
                    "output_tokens": 300,
                    "thinking_tokens": 100,
                    "cached_input_tokens": 50,
                    "estimated_cost_usd": 0.004,
                },
            )
            append_ai_usage_event(
                project_path,
                stage="icon_audit",
                operation="pdf_block_inspection",
                usage={
                    "provider": "openai",
                    "model": "custom-model",
                    "requests": 1,
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "thinking_tokens": 0,
                    "cached_input_tokens": 0,
                    "estimated_cost_usd": None,
                },
            )
            append_ai_usage_event(
                project_path,
                stage="translation",
                operation="draft",
                usage={
                    "model": "priced-model",
                    "requests": 1,
                    "input_tokens": 2_000,
                    "output_tokens": 800,
                    "estimated_cost_usd": 0.01,
                },
            )

            summary = summarize_project_ai_usage(project_path)
            icons = summary["stages"]["source_review"]

            self.assertEqual(icons["events"], 2)
            self.assertEqual(icons["requests"], 3)
            self.assertEqual(icons["input_tokens"], 1_700)
            self.assertEqual(icons["output_tokens"], 400)
            self.assertEqual(icons["total_tokens"], 2_100)
            self.assertEqual(icons["estimated_cost_usd"], 0.004)
            self.assertEqual(icons["unpriced_requests"], 1)
            self.assertFalse(icons["pricing_complete"])
            self.assertEqual(icons["models"], ["custom-model", "priced-model"])
            self.assertEqual(summary["total"]["requests"], 4)
            self.assertEqual(summary["total"]["estimated_cost_usd"], 0.014)

    def test_omits_zero_request_events_and_ignores_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_path = Path(temporary) / "project"
            recorded = append_ai_usage_event(
                project_path,
                stage="icon_audit",
                operation="cache_hit",
                usage={"requests": 0},
            )
            self.assertFalse(recorded)
            ledger = project_path / ".glk/state/ai_usage.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("not-json\n", encoding="utf-8")

            self.assertEqual(
                summarize_project_ai_usage(project_path)["total"]["requests"],
                0,
            )


if __name__ == "__main__":
    unittest.main()
