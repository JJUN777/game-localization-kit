from __future__ import annotations

import unittest
from unittest.mock import patch

from glk.infrastructure.dashboard_routes import (
    DashboardRoute,
    match_dashboard_route,
    registered_dashboard_route_names,
)
from glk.infrastructure.dashboard_server import _DashboardHandler


class DashboardRouteTests(unittest.TestCase):
    def test_every_registered_route_has_one_handler_contract(self) -> None:
        registered = registered_dashboard_route_names()

        self.assertEqual(
            registered,
            _DashboardHandler.handled_route_names,
        )
        self.assertEqual(
            set(registered),
            set(_DashboardHandler.allowed_methods),
        )

    def test_unhandled_matched_route_returns_internal_error(self) -> None:
        route = DashboardRoute(
            name="future_route",
            method="GET",
            path="/future",
            query="",
            access="public",
        )
        handler = object.__new__(_DashboardHandler)
        handler.path = "/future"

        with (
            patch(
                "glk.infrastructure.dashboard_server.match_dashboard_route",
                return_value=route,
            ),
            patch.object(
                handler,
                "_send_unhandled_route",
            ) as send_unhandled,
        ):
            matched = handler._route_request("GET")

        self.assertIsNone(matched)
        send_unhandled.assert_called_once_with(route)

    def test_matches_static_routes_with_their_access_policy(self) -> None:
        cases = [
            ("GET", "/favicon.ico", "favicon", "public"),
            ("GET", "/", "dashboard_ui", "localhost"),
            ("GET", "/api/dashboard", "dashboard", "session"),
            ("GET", "/api/jobs", "jobs", "session"),
            ("GET", "/api/settings/ai", "ai_settings", "session"),
            ("GET", "/api/source-output", "source_output", "session"),
            ("GET", "/api/output", "output", "session"),
            (
                "GET",
                "/api/output-archive",
                "output_archive",
                "session",
            ),
            ("POST", "/api/projects", "projects", "session"),
            ("POST", "/api/review/open", "review_open", "session"),
            ("POST", "/api/jobs/source", "source_job", "session"),
            (
                "POST",
                "/api/jobs/source/continue",
                "source_continue",
                "session",
            ),
            ("POST", "/api/jobs/glossary", "glossary_job", "session"),
            (
                "POST",
                "/api/jobs/translation",
                "translation_job",
                "session",
            ),
            ("PUT", "/api/settings/ai", "ai_settings", "session"),
        ]

        for method, target, expected_name, expected_access in cases:
            with self.subTest(method=method, target=target):
                route = match_dashboard_route(method, target)
                self.assertIsNotNone(route)
                assert route is not None
                self.assertEqual(route.name, expected_name)
                self.assertEqual(route.access, expected_access)
                self.assertIsNone(route.project_id)

    def test_matches_project_routes_and_decodes_the_project_id_once(
        self,
    ) -> None:
        cases = [
            ("POST", "source", "source_upload"),
            ("PUT", "source", "source_upload"),
            ("PATCH", "ocr-prompt", "ocr_prompt"),
            ("PATCH", "translation-prompt", "translation_prompt"),
            (
                "POST",
                "translation-prompt-ai-estimate",
                "translation_prompt_ai_estimate",
            ),
            (
                "POST",
                "translation-prompt-ai-draft",
                "translation_prompt_ai_draft",
            ),
        ]

        for method, action, expected_name in cases:
            with self.subTest(method=method, action=action):
                route = match_dashboard_route(
                    method,
                    f"/api/projects/project%2520name/{action}",
                )
                self.assertIsNotNone(route)
                assert route is not None
                self.assertEqual(route.name, expected_name)
                self.assertEqual(route.project_id, "project%20name")
                self.assertEqual(route.access, "session")

        delete_route = match_dashboard_route(
            "DELETE",
            "/api/projects/project%2520name",
        )
        self.assertIsNotNone(delete_route)
        assert delete_route is not None
        self.assertEqual(delete_route.name, "project_delete")
        self.assertEqual(delete_route.project_id, "project%20name")

    def test_keeps_query_separate_from_route_matching(self) -> None:
        route = match_dashboard_route(
            "GET",
            "/api/output?project_id=demo&path=output%2Ftranslated.txt",
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.path, "/api/output")
        self.assertEqual(
            route.query,
            "project_id=demo&path=output%2Ftranslated.txt",
        )

        archive_route = match_dashboard_route(
            "GET",
            "/api/output-archive?project_id=demo",
        )
        self.assertIsNotNone(archive_route)
        assert archive_route is not None
        self.assertEqual(archive_route.path, "/api/output-archive")
        self.assertEqual(archive_route.query, "project_id=demo")

    def test_rejects_wrong_methods_and_malformed_project_routes(self) -> None:
        cases = [
            ("POST", "/api/dashboard"),
            ("GET", "/api/projects"),
            ("PATCH", "/api/projects/demo/source"),
            ("POST", "/api/projects//source"),
            ("POST", "/api/projects/demo/nested/source"),
            ("DELETE", "/api/projects/"),
            ("DELETE", "/api/projects/demo/nested"),
            ("GET", "/unknown"),
        ]

        for method, target in cases:
            with self.subTest(method=method, target=target):
                self.assertIsNone(match_dashboard_route(method, target))


if __name__ == "__main__":
    unittest.main()
