from __future__ import annotations

import unittest

from glk.extraction.layout import parse_page_selection


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


if __name__ == "__main__":
    unittest.main()
