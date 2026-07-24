from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from glk.application.project_service import create_project
from glk.application.source_review_service import prepare_project_source_review
from glk.infrastructure.dashboard_server import (
    DashboardHttpServer,
    create_dashboard_server,
)
from tests.test_source_review_service import make_block, write_blocks


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        location = create_project(
            name="Dashboard Review",
            workspace_root=self.workspace_root,
        )
        write_blocks(
            location.path / ".glk/segments/source.jsonl",
            [make_block(1, "Review this text.")],
        )
        prepare_project_source_review(
            project="dashboard_review",
            workspace_root=self.workspace_root,
        )
        self.server: DashboardHttpServer = create_dashboard_server(
            workspace_root=self.workspace_root,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        authorized: bool = True,
        origin: str | None = None,
    ) -> tuple[int, dict[str, object] | str]:
        headers: dict[str, str] = {}
        data = None
        if authorized:
            headers["X-GLK-Token"] = self.server.auth_token
        if origin is not None:
            headers["Origin"] = origin
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.server.origin + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=3) as response:
                raw = response.read()
                if response.headers.get_content_type() == "application/json":
                    return response.status, json.loads(raw.decode("utf-8"))
                return response.status, raw.decode("utf-8")
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_serves_dashboard_and_protects_api(self) -> None:
        status, html = self._request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("Game Localization Kit Dashboard", html)
        self.assertIn("data-create-project", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)

        status, unauthorized = self._request(
            "/api/dashboard",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(unauthorized["code"], "REVIEW_SESSION_INVALID")

        status, blocked_origin = self._request(
            "/api/dashboard",
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertFalse(blocked_origin["ok"])

        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["summary"]["projects"], 1)
        project = dashboard["projects"][0]
        self.assertEqual(project["project_id"], "dashboard_review")
        self.assertTrue(project["reviews"]["source"]["enabled"])

    def test_opens_ready_review_and_rejects_unknown_type(self) -> None:
        status, opened = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "dashboard_review",
                "review_type": "source",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(opened["ok"])
        with urlopen(str(opened["url"]), timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn("원문 이미지 · 추출문 검수", html)

        status, reused = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "dashboard_review",
                "review_type": "source",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(reused["url"], opened["url"])

        status, invalid = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "dashboard_review",
                "review_type": "unknown",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "INVALID_REQUEST")

    def test_creates_project_and_rejects_duplicate_id(self) -> None:
        status, created = self._request(
            "/api/projects",
            method="POST",
            payload={
                "name": "Created From Dashboard",
                "project_id": "created_game",
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(created["ok"])
        self.assertEqual(created["project"]["project_id"], "created_game")
        project_path = self.workspace_root / "created_game"
        self.assertTrue((project_path / "project.json").is_file())
        self.assertTrue(
            (project_path / "01_input/images/ocr_prompt.txt").is_file()
        )

        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["summary"]["projects"], 2)

        status, duplicate = self._request(
            "/api/projects",
            method="POST",
            payload={
                "name": "Duplicate",
                "project_id": "created_game",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(duplicate["code"], "PROJECT_INIT_FAILED")
        self.assertIn("이미 존재", duplicate["message"])

    def test_project_creation_requires_a_name(self) -> None:
        status, invalid = self._request(
            "/api/projects",
            method="POST",
            payload={"name": "   "},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "PROJECT_INIT_FAILED")

    def test_project_creation_rejects_non_ascii_project_id(self) -> None:
        status, invalid = self._request(
            "/api/projects",
            method="POST",
            payload={
                "name": "한글 프로젝트",
                "project_id": "한글_프로젝트",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "PROJECT_INIT_FAILED")
        self.assertIn("영문 소문자", invalid["message"])

        status, missing = self._request(
            "/api/projects",
            method="POST",
            payload={"name": "Missing ID"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["code"], "PROJECT_INIT_FAILED")
        self.assertIn("ID를 입력", missing["message"])


if __name__ == "__main__":
    unittest.main()
