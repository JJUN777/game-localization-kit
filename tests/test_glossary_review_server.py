from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from glk.application.glossary_service import build_project_glossary_candidates
from glk.infrastructure.glossary_review_server import (
    GlossaryReviewHttpServer,
    create_glossary_review_server,
)
from tests.test_glossary_service import create_approved_project, sample_blocks


class GlossaryReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        self.project_path = create_approved_project(
            self.workspace_root, sample_blocks()
        )
        build_project_glossary_candidates(
            project="glossary_project",
            workspace_root=self.workspace_root,
        )
        self.server: GlossaryReviewHttpServer = create_glossary_review_server(
            project="glossary_project",
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
    ) -> tuple[int, dict[str, object] | str, dict[str, str]]:
        headers: dict[str, str] = {}
        data = None
        if authorized:
            headers["X-GLK-Token"] = self.server.auth_token
        if origin:
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
                raw = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
                body: dict[str, object] | str = (
                    json.loads(raw)
                    if content_type.startswith("application/json")
                    else raw
                )
                return response.status, body, dict(response.headers.items())
        except HTTPError as error:
            raw = error.read().decode("utf-8")
            return error.code, json.loads(raw), dict(error.headers.items())

    @staticmethod
    def _editable_rows(document: dict[str, object]) -> list[dict[str, object]]:
        return [
            {
                "candidate_id": row["candidate_id"],
                "status": row["status"],
                "source_term": row["source_term"],
                "translation": row["translation"],
                "category": row["category"],
                "note": row["note"],
            }
            for row in document["rows"]
        ]

    def test_serves_table_ui_and_rejects_unauthorized_api(self) -> None:
        status, html, headers = self._request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIsInstance(html, str)
        self.assertIn("용어 후보를 표처럼 검수하세요", html)
        self.assertIn("검증 및 termbase 생성", html)
        self.assertIn("실패 항목: ${payload.detail}", html)
        self.assertIn("원문에 없는 수동 용어라면 화면 상단의", html)
        self.assertIn("승인 원문에 없는 수동 용어 허용", html)
        self.assertLess(
            html.index('id="allow-missing"'),
            html.index('class="toolbar-row search-row"'),
        )
        self.assertIn('id="search-field"', html)
        self.assertIn('<option value="source">원문 용어</option>', html)
        self.assertIn('<option value="translation">번역어</option>', html)
        self.assertIn('<option value="context">출현 문맥</option>', html)
        self.assertIn('<option value="all">전체 항목</option>', html)
        self.assertIn('searchField: "source"', html)
        self.assertNotIn('<option value="note">', html)
        self.assertNotIn("<th>메모</th>", html)
        self.assertLess(
            html.index('class="toolbar-row search-row"'),
            html.index('class="toolbar-row bulk-row"'),
        )
        self.assertLess(
            html.index('id="status-filter"'),
            html.index('id="category-filter"'),
        )
        self.assertLess(
            html.index('id="category-filter"'),
            html.index('id="sort-order"'),
        )
        self.assertLess(
            html.index('id="sort-order"'),
            html.index('id="search-field"'),
        )
        self.assertLess(
            html.index('id="search-field"'),
            html.index('id="search"'),
        )
        self.assertLess(
            html.index('id="bulk-apply"'),
            html.index('id="add-button"'),
        )
        self.assertIn('id="sort-order"', html)
        self.assertIn("첫 등장 위치 순", html)
        self.assertIn("출현 많은 순", html)
        self.assertIn("출현 적은 순", html)
        self.assertIn("용어집 생성이 완료되었습니다", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)
        self.assertNotIn("__GLK_RETURN_URL_JSON__", html)
        self.assertIn("const RETURN_URL = null;", html)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

        status, payload, _ = self._request("/api/review", authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

        status, payload, _ = self._request(
            "/api/review",
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_saves_rows_preserves_evidence_and_detects_conflict(self) -> None:
        status, document, _ = self._request("/api/review")
        self.assertEqual(status, 200)
        original_hash = document["review_sha256"]
        rows = self._editable_rows(document)
        first = rows[0]
        original_source = first["source_term"]
        original_evidence = document["rows"][0]["example"]
        first["status"] = "approved"
        first["translation"] = "시험 번역"
        first["source_term"] = "attempted generated edit"

        status, saved, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": original_hash,
                "rows": rows,
            },
        )
        self.assertEqual(status, 200)
        saved_document = saved["document"]
        self.assertNotEqual(saved_document["review_sha256"], original_hash)
        saved_first = saved_document["rows"][0]
        self.assertEqual(saved_first["source_term"], original_source)
        self.assertEqual(saved_first["example"], original_evidence)
        self.assertEqual(saved_first["translation"], "시험 번역")

        status, conflict, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": original_hash,
                "rows": rows,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["code"], "REVIEW_CONFLICT")
        self.assertIn("새로고침", conflict["message"])
        self.assertIn("changed after", conflict["detail"])

    def test_generated_rows_cannot_be_deleted_and_review_can_be_imported(self) -> None:
        _, document, _ = self._request("/api/review")
        rows = self._editable_rows(document)

        status, payload, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows[1:],
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            payload["code"],
            "GLOSSARY_GENERATED_CANDIDATE_DELETE",
        )
        self.assertIn("삭제할 수 없습니다", payload["message"])
        self.assertIn("cannot be deleted", payload["detail"])

        for row in rows:
            row["status"] = "rejected"
            row["translation"] = ""
        status, imported, _ = self._request(
            "/api/import",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows,
                "allow_missing_terms": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["result"]["active_count"], 0)
        self.assertEqual(imported["document"]["termbase_status"], "current")
        self.assertTrue(
            (self.project_path / "03_terminology/termbase.json").is_file()
        )

    def test_import_error_returns_saved_document_with_current_hash(self) -> None:
        _, document, _ = self._request("/api/review")
        rows = self._editable_rows(document)
        for row in rows:
            row["status"] = "rejected"
        rows[0]["status"] = "review"

        status, payload, _ = self._request(
            "/api/import",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "GLOSSARY_IMPORT_FAILED")
        self.assertIn("검토 중인 용어", payload["message"])
        self.assertIn("still in review", payload["detail"])
        self.assertNotEqual(
            payload["document"]["review_sha256"],
            document["review_sha256"],
        )

    def test_import_error_exposes_the_failed_row_detail(self) -> None:
        _, document, _ = self._request("/api/review")
        rows = self._editable_rows(document)
        for row in rows:
            row["status"] = "rejected"
        rows.insert(
            0,
            {
                "candidate_id": "",
                "status": "approved",
                "source_term": "Missing Manual Term",
                "translation": "누락 수동 용어",
                "category": "term",
                "note": "",
            },
        )

        status, payload, _ = self._request(
            "/api/import",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows,
                "allow_missing_terms": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("Record 2", payload["detail"])
        self.assertIn("Missing Manual Term", payload["detail"])
        self.assertIn("not found in the approved source", payload["detail"])

    def test_injects_only_a_local_return_url(self) -> None:
        return_url = "http://127.0.0.1:8765/"
        server = create_glossary_review_server(
            project="glossary_project",
            workspace_root=self.workspace_root,
            return_url=return_url,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(server.review_url, timeout=3) as response:
                html = response.read().decode("utf-8")
            self.assertIn(
                f"const RETURN_URL = {json.dumps(return_url)};",
                html,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        with self.assertRaisesRegex(ValueError, "local HTTP URL"):
            create_glossary_review_server(
                project="glossary_project",
                workspace_root=self.workspace_root,
                return_url="https://attacker.example/",
            )


if __name__ == "__main__":
    unittest.main()
