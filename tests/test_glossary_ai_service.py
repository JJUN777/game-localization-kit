from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from glk.application.glossary_ai_service import (
    GlossaryAiTriageError,
    estimate_project_glossary_ai_triage,
    get_project_glossary_ai_suggestions,
    triage_project_glossary_candidates,
)
from glk.application.glossary_review_service import (
    GlossaryReviewConflictError,
    get_project_glossary_review_document,
)
from glk.application.glossary_service import build_project_glossary_candidates
from glk.domain.workspace import WorkspacePaths
from glk.extraction.glossary_triage import GLOSSARY_TRIAGE_PROMPT_VERSION
from glk.infrastructure.ai_usage import AiUsageAccumulator
from tests.test_glossary_service import create_approved_project, sample_blocks


class FakeGlossaryTriageProvider:
    model_name = "gemini-3.8-flash"
    prompt_version = GLOSSARY_TRIAGE_PROMPT_VERSION

    def __init__(self) -> None:
        self.usage = AiUsageAccumulator("gemini", self.model_name)
        self.prompts: list[str] = []

    def triage(self, prompt: str) -> dict[str, object]:
        self.prompts.append(prompt)
        self.usage.begin_request()
        candidates = json.loads(prompt.split("Candidates JSON:\n", 1)[1])
        self.usage.input_tokens += 100
        self.usage.output_tokens += 40 * len(candidates)
        suggestions = []
        for index, candidate in enumerate(candidates):
            status = "rejected" if index % 2 == 0 else "approved"
            suggestions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "recommended_status": status,
                    "translation": "" if status == "rejected" else "추천 번역",
                    "category": "term",
                    "confidence": "high",
                    "reason": "용어집 포함 여부를 문맥으로 판단했습니다.",
                }
            )
        return {"suggestions": suggestions}


class GlossaryAiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        self.project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        build_project_glossary_candidates(
            project="glossary_project",
            workspace_root=self.workspace_root,
        )
        self.document = get_project_glossary_review_document(
            project="glossary_project",
            workspace_root=self.workspace_root,
        )
        self.rows = [dict(row) for row in self.document["rows"]]
        self.provider = FakeGlossaryTriageProvider()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def estimate(self):
        return estimate_project_glossary_ai_triage(
            project="glossary_project",
            workspace_root=self.workspace_root,
            settings_root=self.temporary_directory.name,
            expected_review_sha256=self.document["review_sha256"],
            rows=self.rows,
            provider=self.provider,
        )

    def triage(self):
        return triage_project_glossary_candidates(
            project="glossary_project",
            workspace_root=self.workspace_root,
            settings_root=self.temporary_directory.name,
            expected_review_sha256=self.document["review_sha256"],
            rows=self.rows,
            provider=self.provider,
        )

    def test_estimates_only_automatic_review_rows(self) -> None:
        self.rows[0]["status"] = "approved"
        self.rows.append(
            {
                "candidate_id": "",
                "status": "review",
                "source_term": "Manual term",
                "translation": "",
                "category": "term",
                "note": "",
            }
        )

        estimate = self.estimate()

        self.assertEqual(estimate.target_count, len(self.document["rows"]) - 1)
        self.assertEqual(estimate.cached_count, 0)
        self.assertEqual(estimate.request_count, 1)
        self.assertGreater(estimate.estimated_input_tokens, 0)
        self.assertIsNotNone(estimate.estimated_cost_usd_low)
        self.assertGreater(
            estimate.estimated_cost_usd_high,
            estimate.estimated_cost_usd_low,
        )

    def test_triage_caches_results_ledgers_usage_and_avoids_repeat_calls(self) -> None:
        first = self.triage()

        self.assertEqual(first.target_count, len(self.document["rows"]))
        self.assertEqual(first.cached_count, 0)
        self.assertEqual(first.usage["requests"], 1)
        self.assertEqual(len(self.provider.prompts), 1)
        paths = WorkspacePaths(self.project_path)
        self.assertTrue(paths.glossary_ai_review_state.is_file())
        ledger = paths.ai_usage_ledger.read_text(encoding="utf-8")
        self.assertIn('"stage":"glossary"', ledger)
        self.assertIn('"operation":"candidate_triage"', ledger)

        second = self.triage()

        self.assertEqual(second.cached_count, second.target_count)
        self.assertEqual(second.usage["requests"], 0)
        self.assertEqual(len(self.provider.prompts), 1)
        estimate = self.estimate()
        self.assertEqual(estimate.cached_count, estimate.target_count)
        self.assertEqual(estimate.request_count, 0)
        self.assertEqual(estimate.estimated_cost_usd_low, 0.0)

    def test_protects_user_translation_category_and_non_review_status(self) -> None:
        self.rows[0]["translation"] = "직접 번역"
        self.rows[0]["category"] = "ui"
        self.rows[1]["status"] = "keep"

        result = self.triage()

        by_id = {item["candidate_id"]: item for item in result.suggestions}
        first = by_id[self.rows[0]["candidate_id"]]
        self.assertFalse(first["apply_status"])
        self.assertFalse(first["apply_translation"])
        self.assertFalse(first["apply_category"])
        self.assertNotIn(self.rows[1]["candidate_id"], by_id)

    def test_reads_valid_cached_suggestions_without_ai_request(self) -> None:
        self.triage()

        result = get_project_glossary_ai_suggestions(
            project="glossary_project",
            workspace_root=self.workspace_root,
            settings_root=self.temporary_directory.name,
            provider=self.provider,
        )

        self.assertEqual(len(result.suggestions), len(self.document["rows"]))
        self.assertTrue(all(item["cached"] for item in result.suggestions))
        self.assertEqual(result.usage, None)

    def test_rejects_stale_hash_and_missing_generated_rows(self) -> None:
        with self.assertRaisesRegex(GlossaryReviewConflictError, "changed"):
            estimate_project_glossary_ai_triage(
                project="glossary_project",
                workspace_root=self.workspace_root,
                settings_root=self.temporary_directory.name,
                expected_review_sha256="stale",
                rows=self.rows,
                provider=self.provider,
            )
        with self.assertRaisesRegex(GlossaryAiTriageError, "cannot be omitted"):
            estimate_project_glossary_ai_triage(
                project="glossary_project",
                workspace_root=self.workspace_root,
                settings_root=self.temporary_directory.name,
                expected_review_sha256=self.document["review_sha256"],
                rows=self.rows[:-1],
                provider=self.provider,
            )


if __name__ == "__main__":
    unittest.main()
