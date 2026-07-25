from __future__ import annotations

import unittest

from glk.extraction.layout import (
    LayoutValidationError,
    build_page_text,
    join_fragment_texts,
    merge_paragraph_continuations,
    parse_page_selection,
    reconstruct_blocks,
    validate_layout,
)


class ParsePageSelectionTests(unittest.TestCase):
    def test_returns_all_pages_or_a_sorted_unique_selection(self) -> None:
        self.assertEqual(parse_page_selection(None, 5), [0, 1, 2, 3, 4])
        self.assertEqual(
            parse_page_selection("5, 1, 3-5, 3", 5),
            [0, 2, 3, 4],
        )

    def test_rejects_range_endpoints_before_expanding_the_range(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Page range out of range: 1-99999999",
        ):
            parse_page_selection("1-99999999", 10)
        with self.assertRaisesRegex(
            ValueError,
            "Page range out of range: 0-2",
        ):
            parse_page_selection("0-2", 10)

    def test_rejects_invalid_single_pages_and_reversed_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "Page number out of range: 0"):
            parse_page_selection("0", 10)
        with self.assertRaisesRegex(ValueError, "Invalid page range: 5-3"):
            parse_page_selection("5-3", 10)
        with self.assertRaisesRegex(ValueError, "Page number out of range: 11"):
            parse_page_selection("11", 10)

    def test_requires_a_positive_document_page_count(self) -> None:
        for page_count in (0, -1, True):
            with self.subTest(page_count=page_count):
                with self.assertRaisesRegex(
                    ValueError,
                    "Document page count must be a positive integer",
                ):
                    parse_page_selection(None, page_count)


class LayoutRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fragments = [
            {"id": "P001-F001", "text": "First line"},
            {"id": "P001-F002", "text": "continues."},
        ]
        self.layout = {
            "blocks": [
                {
                    "type": "paragraph",
                    "fragment_ids": ["P001-F001", "P001-F002"],
                    "include_in_text": True,
                    "reason": "",
                }
            ]
        }

    def test_validates_and_reconstructs_every_fragment_once(self) -> None:
        report = validate_layout(self.fragments, self.layout)
        blocks = reconstruct_blocks(self.fragments, self.layout)

        self.assertEqual(
            report,
            {
                "valid": True,
                "expected_count": 2,
                "returned_count": 2,
                "missing": [],
                "unknown": [],
                "duplicates": [],
            },
        )
        self.assertEqual(blocks[0]["text"], "First line continues.")

    def test_rejects_missing_unknown_and_duplicate_fragments(self) -> None:
        cases = (
            (["P001-F001"], "'missing': \\['P001-F002'\\]"),
            (
                ["P001-F001", "P001-F002", "P999-F999"],
                "'unknown': \\['P999-F999'\\]",
            ),
            (
                ["P001-F001", "P001-F001", "P001-F002"],
                "'duplicates': \\['P001-F001'\\]",
            ),
        )
        for fragment_ids, message in cases:
            layout = {
                "blocks": [
                    {
                        **self.layout["blocks"][0],
                        "fragment_ids": fragment_ids,
                    }
                ]
            }
            with self.subTest(fragment_ids=fragment_ids):
                with self.assertRaisesRegex(LayoutValidationError, message):
                    validate_layout(self.fragments, layout)

    def test_rejects_malformed_layout_block_fields(self) -> None:
        invalid_blocks = (
            {"type": "paragraph"},
            {
                **self.layout["blocks"][0],
                "type": "unsupported",
            },
            {
                **self.layout["blocks"][0],
                "include_in_text": "yes",
            },
            {
                **self.layout["blocks"][0],
                "reason": None,
            },
            {
                **self.layout["blocks"][0],
                "fragment_ids": [1, 2],
            },
        )
        for block in invalid_blocks:
            with self.subTest(block=block):
                with self.assertRaises(LayoutValidationError):
                    validate_layout(self.fragments, {"blocks": [block]})

    def test_join_fragment_texts_handles_wraps_and_spacing(self) -> None:
        self.assertEqual(
            join_fragment_texts(["multi-", "player", "", "  rule   text  "]),
            "multiplayer rule text",
        )
        self.assertEqual(
            join_fragment_texts(["Cost /", "value"]),
            "Cost /value",
        )

    def test_paragraph_merge_stops_at_semantic_boundaries(self) -> None:
        base = {
            "type": "paragraph",
            "fragment_ids": ["P001-F001"],
            "include_in_text": True,
            "reason": "",
            "text": "Complete sentence.",
        }
        cases = (
            ({**base, "text": "Complete sentence."}, "lowercase continuation"),
            ({**base, "text": "Open sentence"}, "Uppercase continuation"),
            (
                {**base, "text": "Open sentence", "include_in_text": False},
                "lowercase continuation",
            ),
            ({**base, "text": "Open sentence"}, ""),
        )
        for first, second_text in cases:
            second = {
                **base,
                "fragment_ids": ["P001-F002"],
                "text": second_text,
            }
            with self.subTest(first=first, second=second):
                self.assertEqual(
                    len(merge_paragraph_continuations([first, second])),
                    2,
                )

    def test_build_page_text_uses_only_included_non_empty_blocks(self) -> None:
        blocks = [
            {"text": "First", "include_in_text": True},
            {"text": "Decoration", "include_in_text": False},
            {"text": "", "include_in_text": True},
            {"text": "Second", "include_in_text": True},
        ]

        self.assertEqual(build_page_text(blocks), "First\n\nSecond")


if __name__ == "__main__":
    unittest.main()
