from __future__ import annotations

import unittest

from glk.extraction.image_ocr import (
    ImageOcrValidationError,
    build_combined_text,
    build_individual_text,
    build_ocr_prompt,
    validate_ocr_result,
)


class ImageOcrTests(unittest.TestCase):
    def test_prompt_contains_common_and_per_image_instructions(self) -> None:
        prompt = build_ocr_prompt(
            "- [DEF]: an empty shield icon.",
            "Read the small footer text.",
        )
        self.assertIn("- [DEF]: an empty shield icon.", prompt)
        self.assertIn("Read the small footer text.", prompt)
        self.assertIn("Do not translate", prompt)
        self.assertIn("written visual", prompt)
        self.assertIn("[DAMAGE]", prompt)
        self.assertNotIn("Reference icon images", prompt)

    def test_valid_result_builds_individual_text(self) -> None:
        result = validate_ocr_result(
            {
                "blocks": [
                    {
                        "type": "title",
                        "text": "FIREBALL",
                        "bbox": [100, 50, 900, 180],
                        "legibility": "clear",
                    },
                    {
                        "type": "body",
                        "text": "Deal 3 [FIRE].",
                        "bbox": [150, 500, 850, 700],
                        "legibility": "clear",
                    },
                ],
                "warnings": [],
            }
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            build_individual_text(result["blocks"]),
            "FIREBALL\n\nDeal 3 [FIRE].",
        )

    def test_uncertain_or_empty_result_needs_review(self) -> None:
        uncertain = validate_ocr_result(
            {
                "blocks": [
                    {
                        "type": "body",
                        "text": "[ILLEGIBLE]",
                        "bbox": [0, 0, 1000, 1000],
                        "legibility": "uncertain",
                    }
                ],
                "warnings": ["Small text"],
            }
        )
        empty = validate_ocr_result({"blocks": [], "warnings": ["No text"]})
        self.assertEqual(uncertain["status"], "needs_review")
        self.assertEqual(empty["status"], "needs_review")

    def test_invalid_bbox_is_rejected(self) -> None:
        with self.assertRaises(ImageOcrValidationError):
            validate_ocr_result(
                {
                    "blocks": [
                        {
                            "type": "body",
                            "text": "Text",
                            "bbox": [500, 0, 100, 1000],
                            "legibility": "clear",
                        }
                    ],
                    "warnings": [],
                }
            )

    def test_combined_text_uses_exact_separator_format(self) -> None:
        combined = build_combined_text([("a.txt", "Alpha"), ("b.txt", "")])
        self.assertEqual(
            combined,
            "[a.txt]\nAlpha\n\n======================\n\n"
            "[b.txt]\n\n======================",
        )


if __name__ == "__main__":
    unittest.main()
