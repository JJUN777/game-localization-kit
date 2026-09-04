from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from glk.application.translation_prompt_ai_service import (
    TranslationPromptAiError,
    _representative_samples,
    estimate_translation_prompt_draft,
    generate_translation_prompt_draft,
)
from glk.domain.workspace import WorkspacePaths
from glk.extraction.translation_prompt_draft import (
    TRANSLATION_PROMPT_DRAFT_VERSION,
)
from glk.infrastructure.ai_usage import AiUsageAccumulator
from tests.test_glossary_service import create_approved_project, sample_blocks


class FakeTranslationPromptDraftProvider:
    provider_name = "gemini"
    model_name = "gemini-3.8-flash"
    prompt_version = TRANSLATION_PROMPT_DRAFT_VERSION

    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.prompts: list[str] = []
        self.usage = AiUsageAccumulator(self.provider_name, self.model_name)

    def generate_draft(self, prompt: str) -> dict[str, str]:
        self.prompts.append(prompt)
        self.usage.begin_request()
        self.usage.input_tokens += 320
        self.usage.output_tokens += 140
        if self.invalid:
            return {"draft": "한 줄", "rationale": "형식 오류"}
        return {
            "draft": (
                "이 게임은 도시를 건설하고 관리하는 보드게임이며, 이 문서는 해당 게임 규칙서의 한국어 번역입니다.\n"
                "규칙 본문은 간결한 격식체로 번역하세요.\n"
                "제목은 짧고 명확한 명사구로 작성하세요.\n"
                "영어식 어순과 불필요한 주어 반복을 피하세요.\n"
                "조건과 예외의 적용 순서를 분명하게 유지하세요."
            ),
            "rationale": "명령형 규칙과 짧은 제목이 반복되는 문서입니다.",
        }


class TranslationPromptAiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        self.project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        self.settings_root = Path(self.temporary_directory.name) / "settings"
        self.provider = FakeTranslationPromptDraftProvider()
        self.current_prompt = "자연스러운 한국어 규칙서 문체로 번역하세요."

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def estimate(self, *, force: bool = False):
        return estimate_translation_prompt_draft(
            project="glossary_project",
            workspace_root=self.workspace_root,
            settings_root=self.settings_root,
            current_prompt=self.current_prompt,
            provider=self.provider,
            force=force,
        )

    def generate(self, provider=None, *, force: bool = False):
        return generate_translation_prompt_draft(
            project="glossary_project",
            workspace_root=self.workspace_root,
            settings_root=self.settings_root,
            current_prompt=self.current_prompt,
            provider=provider or self.provider,
            force=force,
        )

    def test_estimates_tokens_cost_and_uses_bounded_source_samples(self) -> None:
        estimate = self.estimate()

        self.assertEqual(estimate.request_count, 1)
        self.assertFalse(estimate.cached)
        self.assertGreater(estimate.sample_count, 0)
        self.assertGreater(estimate.estimated_input_tokens, 0)
        self.assertGreater(
            estimate.estimated_output_tokens_high,
            estimate.estimated_output_tokens_low,
        )
        self.assertIsNotNone(estimate.estimated_cost_usd_low)

    def test_sends_opening_pages_then_distributed_later_style_samples(
        self,
    ) -> None:
        blocks = [
            replace(
                sample_blocks()[0],
                id=f"page-{page}-block-{position}",
                page=page,
                source_order=(page * 10) + position,
                block_order=position,
                block_type="heading" if position == 0 else "body",
                raw_text=f"PAGE_{page}_BLOCK_{position}",
                source_hash=f"sha256:{page:02d}{position:02d}",
            )
            for page in range(1, 9)
            for position in range(3)
        ]

        samples = _representative_samples(blocks)
        opening = [
            sample for sample in samples if sample["role"] == "opening_context"
        ]
        later = [
            sample
            for sample in samples
            if sample["role"] == "later_style_sample"
        ]

        self.assertEqual({sample["page"] for sample in opening}, {1, 2, 3, 4})
        self.assertEqual(len(opening), 12)
        self.assertLessEqual(len(later), 8)
        self.assertTrue(all(int(sample["page"]) >= 5 for sample in later))

    def test_generates_caches_and_ledgers_actual_usage_without_glossary(self) -> None:
        paths = WorkspacePaths(self.project_path)
        paths.termbase.write_text(
            json.dumps({"entries": [{"source_term": "SECRET_GLOSSARY_TERM"}]}),
            encoding="utf-8",
        )

        first = self.generate()

        self.assertFalse(first.cached)
        self.assertTrue(
            first.draft.startswith(
                "이 게임은 도시를 건설하고 관리하는 보드게임이며"
            )
        )
        self.assertEqual(first.usage["requests"], 1)
        self.assertEqual(first.usage["input_tokens"], 320)
        self.assertEqual(first.usage["output_tokens"], 140)
        self.assertEqual(len(self.provider.prompts), 1)
        self.assertIn("Glossary Project", self.provider.prompts[0])
        self.assertIn("board_game_rulebook", self.provider.prompts[0])
        self.assertNotIn("SECRET_GLOSSARY_TERM", self.provider.prompts[0])
        self.assertTrue(paths.translation_prompt_ai_draft_state.is_file())
        ledger = paths.ai_usage_ledger.read_text(encoding="utf-8")
        self.assertIn('"stage":"translation"', ledger)
        self.assertIn('"operation":"prompt_draft"', ledger)

        second = self.generate()

        self.assertTrue(second.cached)
        self.assertIsNone(second.usage)
        self.assertEqual(len(self.provider.prompts), 1)
        estimate = self.estimate()
        self.assertTrue(estimate.cached)
        self.assertEqual(estimate.cached_result["draft"], first.draft)
        self.assertEqual(estimate.request_count, 0)
        self.assertEqual(estimate.estimated_input_tokens, 0)
        self.assertEqual(estimate.estimated_cost_usd_low, 0.0)

        forced_estimate = self.estimate(force=True)
        self.assertFalse(forced_estimate.cached)
        self.assertIsNone(forced_estimate.cached_result)
        self.assertEqual(forced_estimate.request_count, 1)
        self.assertGreater(forced_estimate.estimated_input_tokens, 0)

        forced = self.generate(force=True)
        self.assertFalse(forced.cached)
        self.assertEqual(len(self.provider.prompts), 2)

    def test_current_prompt_change_invalidates_cache(self) -> None:
        self.generate()
        self.current_prompt += " 제목은 더 짧게 작성하세요."

        self.generate()

        self.assertEqual(len(self.provider.prompts), 2)

    def test_rejects_invalid_provider_response_without_caching(self) -> None:
        invalid = FakeTranslationPromptDraftProvider(invalid=True)

        with self.assertRaises(TranslationPromptAiError) as raised:
            self.generate(invalid)

        self.assertEqual(
            raised.exception.code,
            "TRANSLATION_PROMPT_AI_RESPONSE_INVALID",
        )
        self.assertFalse(
            WorkspacePaths(
                self.project_path
            ).translation_prompt_ai_draft_state.exists()
        )


if __name__ == "__main__":
    unittest.main()
