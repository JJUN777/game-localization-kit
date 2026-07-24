from __future__ import annotations

import unittest

from glk.error_response import make_error_response, make_http_error_response


class ErrorResponseTests(unittest.TestCase):
    def test_cli_error_has_stable_code_korean_message_and_original_detail(self) -> None:
        error = make_error_response(
            "TRANSLATION_FAILED",
            "Termbase is not current.",
        )

        self.assertEqual(error.code, "TRANSLATION_FAILED")
        self.assertIn("용어집", error.message)
        self.assertEqual(error.detail, "Termbase is not current.")
        self.assertEqual(
            error.to_dict(),
            {
                "ok": False,
                "code": "TRANSLATION_FAILED",
                "message": error.message,
                "detail": "Termbase is not current.",
            },
        )

    def test_review_conflict_gets_specific_code_and_recovery_message(self) -> None:
        error = make_http_error_response(
            409,
            "Translation review changed after this page was loaded.",
        )

        self.assertEqual(error.code, "REVIEW_CONFLICT")
        self.assertIn("새로고침", error.message)
        self.assertIn("changed after", error.detail or "")

    def test_unknown_error_code_uses_safe_korean_fallback(self) -> None:
        error = make_error_response("UNKNOWN_FAILURE", "technical failure")

        self.assertEqual(error.message, "요청을 처리하지 못했습니다.")
        self.assertEqual(error.detail, "technical failure")


if __name__ == "__main__":
    unittest.main()
