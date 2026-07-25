from __future__ import annotations

from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from glk.infrastructure.dashboard_server import _DashboardHandler
from glk.infrastructure.glossary_review_server import _GlossaryReviewHandler
from glk.infrastructure.source_review_server import _SourceReviewHandler
from glk.infrastructure.translation_review_server import (
    _TranslationReviewHandler,
)


class SessionTokenSecurityTests(unittest.TestCase):
    def _handler(self, handler_type: type) -> object:
        handler = object.__new__(handler_type)
        headers = Message()
        headers["Host"] = "127.0.0.1"
        headers["X-GLK-Token"] = "session-token"
        handler.headers = headers
        handler.server = SimpleNamespace(
            auth_token="session-token",
            origin="http://127.0.0.1:8765",
        )
        return handler

    def test_all_api_handlers_compare_session_tokens_in_constant_time(
        self,
    ) -> None:
        handler_types = (
            _DashboardHandler,
            _SourceReviewHandler,
            _GlossaryReviewHandler,
            _TranslationReviewHandler,
        )
        for handler_type in handler_types:
            with self.subTest(handler=handler_type.__name__):
                handler = self._handler(handler_type)
                with patch(
                    "secrets.compare_digest",
                    return_value=True,
                ) as compare:
                    self.assertTrue(handler._api_authorized())

                compare.assert_called_once_with(
                    "session-token",
                    "session-token",
                )

    def test_source_asset_token_uses_constant_time_comparison(self) -> None:
        handler = self._handler(_SourceReviewHandler)
        with patch(
            "secrets.compare_digest",
            return_value=True,
        ) as compare:
            self.assertTrue(
                handler._asset_authorized({"token": ["session-token"]})
            )

        compare.assert_called_once_with("session-token", "session-token")


if __name__ == "__main__":
    unittest.main()
