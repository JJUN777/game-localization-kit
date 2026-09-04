from __future__ import annotations

import unittest

from glk.extraction.translation_prompt_draft import (
    TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
    TranslationPromptDraftValidationError,
    build_translation_prompt_draft_request,
    validate_translation_prompt_draft_result,
)


class TranslationPromptDraftTests(unittest.TestCase):
    def test_builds_request_without_glossary_section(self) -> None:
        prompt = build_translation_prompt_draft_request(
            project_name="Concrete City",
            source_format="pdf",
            source_language="en",
            target_language="ko",
            current_prompt="자연스럽게 번역하세요.",
            samples=[{"type": "body", "page": 1, "source": "Take a card."}],
        )

        self.assertIn("Concrete City", prompt)
        self.assertIn('"document_type":"board_game_rulebook"', prompt)
        self.assertIn('"source_format":"pdf"', prompt)
        self.assertIn('"target_language":"ko"', prompt)
        self.assertIn("Take a card.", prompt)
        self.assertIn("대표 원문 데이터 JSON", prompt)
        self.assertNotIn("termbase", prompt.casefold())
        self.assertNotIn("glossary", prompt.casefold())
        self.assertIn("'합니다체' 설명문체", prompt)
        self.assertIn("격식 있는 설명문체", TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION)
        self.assertIn("~해야 합니다", TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION)
        self.assertIn("~할 수 있습니다", TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION)
        self.assertIn("~할 수 없습니다", TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION)
        self.assertIn(
            "draft의 첫 줄에는",
            TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )
        self.assertIn(
            "게임명과 대표 원문을 바탕으로",
            TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )
        self.assertIn(
            "opening_context",
            TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )
        self.assertIn(
            "later_style_sample",
            TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )
        self.assertIn(
            "신뢰할 수 없는 데이터",
            TRANSLATION_PROMPT_DRAFT_SYSTEM_INSTRUCTION,
        )

    def test_normalizes_valid_draft_and_rejects_bad_shape(self) -> None:
        result = validate_translation_prompt_draft_result(
            {
                "draft": " 첫째 지침입니다.\r\n둘째 지침입니다.\n셋째 지침입니다. ",
                "rationale": "  규칙서 문체를   반영했습니다. ",
            }
        )

        self.assertEqual(
            result["draft"],
            "첫째 지침입니다.\n둘째 지침입니다.\n셋째 지침입니다.",
        )
        self.assertEqual(result["rationale"], "규칙서 문체를 반영했습니다.")
        with self.assertRaises(TranslationPromptDraftValidationError):
            validate_translation_prompt_draft_result(
                {"draft": "한 줄뿐입니다.", "rationale": "부족합니다."}
            )


if __name__ == "__main__":
    unittest.main()
