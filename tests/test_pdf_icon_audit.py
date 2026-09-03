from __future__ import annotations

import unittest

from glk.extraction.pdf_icon_audit import (
    PdfIconAuditValidationError,
    build_pdf_icon_audit_prompt,
    icon_token_definitions,
    insert_icon_markers,
    text_units,
    validate_pdf_icon_audit_result,
)


class PdfIconAuditTests(unittest.TestCase):
    def test_extracts_only_explicit_icon_token_definitions(self) -> None:
        prompt = """
- [DAMAGE]: orange diamond with no inner mark.
- [DEFENSE]: empty shield outline.
Ignore [NOT_A_DEFINITION] in prose.
- {LEGACY}: legacy braces are not icon definitions.
"""

        self.assertEqual(
            icon_token_definitions(prompt),
            {
                "DAMAGE": "orange diamond with no inner mark.",
                "DEFENSE": "empty shield outline.",
            },
        )

    def test_validates_anchor_and_inserts_marker_without_rewriting_text(self) -> None:
        text = "Gain 2 damage."
        self.assertEqual(
            [item["text"] for item in text_units(text)],
            ["Gain", "2", "damage", "."],
        )
        result = validate_pdf_icon_audit_result(
            {
                "icons": [
                    {
                        "marker": "[DAMAGE]",
                        "description": "orange diamond",
                        "after_unit_id": "U002",
                        "confidence": "high",
                    }
                ],
                "summary": "One icon found.",
            },
            text=text,
            token_definitions={"DAMAGE": "orange diamond"},
        )

        self.assertEqual(
            insert_icon_markers(text, result["icons"]),
            "Gain 2 [DAMAGE] damage.",
        )

    def test_rejects_undefined_tokens_and_unknown_anchors(self) -> None:
        base = {
            "marker": "[DAMAGE]",
            "description": "orange diamond",
            "after_unit_id": "U001",
            "confidence": "high",
        }
        with self.assertRaisesRegex(PdfIconAuditValidationError, "undefined"):
            validate_pdf_icon_audit_result(
                {"icons": [base], "summary": ""},
                text="Gain damage.",
                token_definitions={},
            )
        with self.assertRaisesRegex(PdfIconAuditValidationError, "anchor"):
            validate_pdf_icon_audit_result(
                {
                    "icons": [
                        {
                            **base,
                            "marker": "[ICON: orange diamond]",
                            "after_unit_id": "U999",
                        }
                    ],
                    "summary": "",
                },
                text="Gain damage.",
                token_definitions={},
            )

    def test_fixed_prompt_limits_ai_to_insertion_markers(self) -> None:
        prompt = build_pdf_icon_audit_prompt(
            page=4,
            block_id="pdf-p0004-b0002",
            text="Gain 2 damage.",
            target_bbox=(10, 20, 200, 90),
            token_definitions={"DAMAGE": "orange diamond"},
        )

        self.assertIn("This is an icon audit, not OCR", prompt)
        self.assertIn("Never rewrite", prompt)
        self.assertIn('"id":"U002","text":"2"', prompt)
        self.assertIn('"DAMAGE": "orange diamond"', prompt)


if __name__ == "__main__":
    unittest.main()
