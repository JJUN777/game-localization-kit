from __future__ import annotations

import unittest

from glk.domain.translation_qa import check_translation_contract


def _issue_codes(
    source_text: str,
    translated_text: str,
    termbase_entries: list[dict[str, object]] | None = None,
) -> list[str]:
    return [
        issue.code
        for issue in check_translation_contract(
            source_text=source_text,
            translated_text=translated_text,
            termbase_entries=termbase_entries or [],
        )
    ]


class TranslationContractTests(unittest.TestCase):
    def test_reports_each_changed_preserved_item_group(self) -> None:
        codes = _issue_codes(
            "Gain {count} [ICON:coin] <b>2,000</b> points at 50%.",
            "코인 {amount} [ICON:gem] <i>3,000</i>개를 40% 확률로 얻습니다.",
        )

        self.assertEqual(
            codes,
            [
                "curly_token_changed",
                "square_token_changed",
                "html_tag_changed",
                "number_changed",
            ],
        )

    def test_preserved_item_counts_must_match(self) -> None:
        self.assertEqual(
            _issue_codes(
                "Spend {coin}, then gain {coin}.",
                "{coin}을 사용한 뒤 코인을 얻습니다.",
            ),
            ["curly_token_changed"],
        )

    def test_allows_number_words_and_implicit_korean_singular_counter(self) -> None:
        self.assertEqual(
            _issue_codes(
                "Draw one card, then gain five points.",
                "카드 1장을 뽑고 5점을 얻습니다.",
            ),
            [],
        )
        self.assertEqual(
            _issue_codes(
                "Reveal the event card.",
                "이벤트 카드 1장을 공개합니다.",
            ),
            [],
        )

    def test_approved_term_accepts_a_source_variant_and_requires_translation(
        self,
    ) -> None:
        entries = [
            {
                "source_term": "attack",
                "translation": "공격",
                "status": "approved",
                "variants": ["attacks"],
            }
        ]

        self.assertEqual(
            _issue_codes("The hero attacks.", "영웅이 공격합니다.", entries),
            [],
        )
        self.assertEqual(
            _issue_codes("The hero attacks.", "영웅이 행동합니다.", entries),
            ["approved_term_missing"],
        )

    def test_keep_term_requires_the_matching_source_variant(self) -> None:
        entries = [
            {
                "source_term": "player",
                "translation": "player",
                "status": "keep",
                "variants": ["players"],
            }
        ]

        self.assertEqual(
            _issue_codes("All players act.", "All players가 행동합니다.", entries),
            [],
        )
        self.assertEqual(
            _issue_codes("All players act.", "각 player가 행동합니다.", entries),
            ["keep_term_changed"],
        )

    def test_term_matching_uses_word_boundaries_and_ignores_rejected_entries(
        self,
    ) -> None:
        entries = [
            {
                "source_term": "player",
                "translation": "플레이어",
                "status": "approved",
                "variants": [],
            },
            {
                "source_term": "mode",
                "translation": "모드",
                "status": "rejected",
                "variants": [],
            },
        ]

        self.assertEqual(
            _issue_codes(
                "This multiplayer mode is optional.",
                "이 선택 규칙을 사용할 수 있습니다.",
                entries,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
