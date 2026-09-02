from __future__ import annotations

from email.message import Message
from http import HTTPStatus
from http.client import HTTPConnection
from io import BytesIO
import threading
import unittest

from glk.application.glossary_review_service import GlossaryReviewError
from glk.application.source_review_service import SourceReviewError
from glk.application.translation_review_service import TranslationReviewError
from glk.infrastructure.dashboard_server import (
    DashboardError,
    DashboardHttpServer,
    _DashboardHandler,
    create_dashboard_server,
)
from glk.infrastructure.glossary_review_server import (
    GlossaryReviewHttpServer,
    _GlossaryReviewHandler,
    create_glossary_review_server,
)
from glk.infrastructure.local_http import (
    LocalHttpRequestHandler,
    LocalHttpServer,
    local_security_headers,
    validate_local_port,
    validate_local_return_url,
)
from glk.infrastructure.source_review_server import (
    SourceReviewHttpServer,
    _SourceReviewHandler,
    create_source_review_server,
)
from glk.infrastructure.translation_review_server import (
    TranslationReviewHttpServer,
    _TranslationReviewHandler,
    create_translation_review_server,
)


class TestRequestError(ValueError):
    pass


class LocalHttpFoundationTests(unittest.TestCase):
    def test_all_local_servers_and_handlers_use_the_shared_base(self) -> None:
        for server_type in (
            DashboardHttpServer,
            SourceReviewHttpServer,
            GlossaryReviewHttpServer,
            TranslationReviewHttpServer,
        ):
            with self.subTest(server=server_type.__name__):
                self.assertTrue(issubclass(server_type, LocalHttpServer))

        for handler_type in (
            _DashboardHandler,
            _SourceReviewHandler,
            _GlossaryReviewHandler,
            _TranslationReviewHandler,
        ):
            with self.subTest(handler=handler_type.__name__):
                self.assertTrue(
                    issubclass(handler_type, LocalHttpRequestHandler)
                )

    def test_security_headers_only_allow_blob_images_when_requested(self) -> None:
        standard = local_security_headers()
        source_review = local_security_headers(allow_blob_images=True)

        self.assertNotIn("blob:", standard["Content-Security-Policy"])
        self.assertIn("blob:", source_review["Content-Security-Policy"])
        for headers in (standard, source_review):
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertIn("style-src 'self'", headers["Content-Security-Policy"])

    def test_all_handlers_secure_unsupported_method_responses(self) -> None:
        handler_types = (
            _DashboardHandler,
            _SourceReviewHandler,
            _GlossaryReviewHandler,
            _TranslationReviewHandler,
        )
        for handler_type in handler_types:
            with self.subTest(handler=handler_type.__name__):
                server = LocalHttpServer(
                    ("127.0.0.1", 0),
                    handler_type,
                )
                thread = threading.Thread(
                    target=server.serve_forever,
                    daemon=True,
                )
                thread.start()
                try:
                    for method in ("HEAD", "OPTIONS", "BREW"):
                        with self.subTest(method=method):
                            connection = HTTPConnection(
                                "127.0.0.1",
                                server.server_port,
                                timeout=3,
                            )
                            connection.request(method, "/")
                            response = connection.getresponse()
                            body = response.read()
                            headers = dict(response.getheaders())
                            connection.close()

                            self.assertEqual(
                                response.status,
                                HTTPStatus.METHOD_NOT_ALLOWED,
                            )
                            self.assertEqual(headers["Server"], "GLK")
                            self.assertNotIn(
                                "Python",
                                "\n".join(headers.values()),
                            )
                            self.assertEqual(
                                headers["X-Frame-Options"],
                                "DENY",
                            )
                            self.assertEqual(
                                headers["Cache-Control"],
                                "no-store",
                            )
                            self.assertIn(
                                "Content-Security-Policy",
                                headers,
                            )
                            self.assertEqual(
                                headers["Allow"],
                                ", ".join(handler_type.allowed_methods),
                            )
                            if method == "HEAD":
                                self.assertEqual(body, b"")
                            else:
                                self.assertIn(
                                    b"METHOD_NOT_ALLOWED",
                                    body,
                                )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_port_validation_rejects_bool_and_out_of_range_values(self) -> None:
        self.assertEqual(validate_local_port(0), 0)
        self.assertEqual(validate_local_port(65535), 65535)
        for value in (True, -1, 65536, "8765"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 0 and 65535"):
                    validate_local_port(value)

    def test_all_server_factories_validate_the_port_first(self) -> None:
        factories = (
            (create_dashboard_server, DashboardError, {}),
            (
                create_source_review_server,
                SourceReviewError,
                {"project": "missing"},
            ),
            (
                create_glossary_review_server,
                GlossaryReviewError,
                {"project": "missing"},
            ),
            (
                create_translation_review_server,
                TranslationReviewError,
                {"project": "missing"},
            ),
        )
        for factory, error_type, kwargs in factories:
            with self.subTest(factory=factory.__name__):
                with self.assertRaisesRegex(
                    error_type,
                    "between 0 and 65535",
                ):
                    factory(port=True, **kwargs)

    def test_return_url_validation_accepts_only_local_http(self) -> None:
        valid = "http://127.0.0.1:8765/"
        self.assertEqual(
            validate_local_return_url(valid, label="Review"),
            valid,
        )
        for value in (
            "https://127.0.0.1/",
            "http://attacker.example/",
            "http://user@localhost/",
            "http://localhost/?next=bad",
            "http://localhost/#bad",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "Review return URL",
                ):
                    validate_local_return_url(value, label="Review")

    def test_json_reader_raises_the_configured_error_type(self) -> None:
        handler = object.__new__(LocalHttpRequestHandler)
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = "2"
        handler.headers = headers
        handler.rfile = BytesIO(b"[]")
        handler.request_error_type = TestRequestError

        with self.assertRaisesRegex(TestRequestError, "JSON object"):
            handler._read_request_json(max_bytes=16)


if __name__ == "__main__":
    unittest.main()
