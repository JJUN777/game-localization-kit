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
from tests.test_glossary_ai_service import FakeGlossaryTriageProvider
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
            settings_root=self.temporary_directory.name,
            glossary_ai_provider=FakeGlossaryTriageProvider(),
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
        self.assertIn("번역에 사용할 용어를 정리하세요", html)
        self.assertIn(
            "용어별 상태·번역어·분류를 확인하고, 검수가 끝나면 용어집을 확정하세요.",
            html,
        )
        self.assertNotIn("glossary_review.tsv에 그대로 저장", html)
        self.assertIn("용어집 확정", html)
        self.assertIn("후보 1차 정리(AI)", html)
        self.assertIn("아직 검토하지 않은 자동 후보만 AI가 분류합니다.", html)
        self.assertIn("AI 적용 취소", html)
        self.assertIn('id="aiResultDialog"', html)
        self.assertIn('body: requestBody()', html)
        self.assertIn('row.status !== "review"', html)
        self.assertIn("입력과 다른 추천 ${conflictCount}", html)
        self.assertIn('conflicts.length ? " · 입력과 다름" : ""', html)
        self.assertIn("현재 번역어 '${currentTranslation}' 유지", html)
        self.assertIn("현재 분류 '${CATEGORY_LABELS[item.current_category]}' 유지", html)
        self.assertIn("AI 추천으로 변경", html)
        self.assertIn("state.aiConflictSelections.has(", html)
        self.assertIn("추천 반영 · AI 값 ${selected}개 포함", html)
        self.assertIn("실패 항목: ${payload.detail}", html)
        self.assertNotIn('id="allow-missing"', html)
        self.assertIn("원문에서 찾을 수 없는 수동 용어가 있습니다", html)
        self.assertIn("돌아가서 수정", html)
        self.assertIn("그대로 포함하고 확정", html)
        self.assertIn('payload.code === "GLOSSARY_MANUAL_TERMS_MISSING"', html)
        self.assertIn('current: "최신 상태"', html)
        self.assertIn('not_built: "미생성"', html)
        self.assertIn('stale: "업데이트 필요"', html)
        self.assertIn('not_ready: "준비 전"', html)
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
            html.index('class="toolbar-row bulk-row hidden"'),
        )
        self.assertLess(
            html.index('id="status-filters"'),
            html.index('id="category-filter"'),
        )
        self.assertNotIn('<select class="control" id="status-filter"', html)
        self.assertIn('button.dataset.statusFilter = value;', html)
        self.assertIn('button.setAttribute("aria-pressed", String(active));', html)
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
            html.index('id="add-button"'),
            html.index('class="toolbar-row bulk-row hidden"'),
        )
        self.assertIn(
            'class="button button-primary" id="import-button"',
            html,
        )
        self.assertNotIn(
            'class="button button-success" id="import-button"',
            html,
        )
        self.assertIn('id="clear-selection"', html)
        self.assertIn(
            'id="bulk-apply" type="button" disabled',
            html,
        )
        self.assertIn('item.manual ? "확정 시 원문 확인"', html)
        self.assertNotIn('"저장 후 확인"', html)
        self.assertIn(
            '$("#search-row").classList.toggle("hidden", hasSelection);',
            html,
        )
        self.assertIn(
            '$("#bulk-row").classList.toggle("hidden", !hasSelection);',
            html,
        )
        self.assertIn(
            '$("#bulk-apply").disabled = !hasSelection || !$("#bulk-status").value;',
            html,
        )
        self.assertIn('state.selected.clear();\n      renderRows();', html)
        self.assertIn('id="sort-order"', html)
        self.assertIn("첫 등장 위치 순", html)
        self.assertIn("출현 많은 순", html)
        self.assertIn("출현 적은 순", html)
        self.assertIn("용어집 확정 완료", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)
        self.assertNotIn("__GLK_RETURN_URL_JSON__", html)
        self.assertIn("const RETURN_URL = null;", html)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

        status, payload, _ = self._request("/api/review", authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

        status, payload, _ = self._request("/api/ai-triage", authorized=False)
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

    def test_estimates_runs_and_reloads_cached_ai_triage_without_saving(self) -> None:
        _, document, _ = self._request("/api/review")
        rows = self._editable_rows(document)

        status, estimate, _ = self._request(
            "/api/ai-triage/estimate",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(estimate["target_count"], len(rows))
        self.assertEqual(estimate["request_count"], 1)
        self.assertEqual(estimate["model"], "gemini-3.8-flash")

        status, result, _ = self._request(
            "/api/ai-triage",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "rows": rows,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["suggestions"]), len(rows))
        self.assertEqual(result["usage"]["requests"], 1)

        _, after, _ = self._request("/api/review")
        self.assertEqual(after["review_sha256"], document["review_sha256"])
        self.assertTrue(all(row["status"] == "review" for row in after["rows"]))

        status, cached, _ = self._request("/api/ai-triage")
        self.assertEqual(status, 200)
        self.assertEqual(cached["cached_count"], len(rows))
        self.assertTrue(all(item["cached"] for item in cached["suggestions"]))

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

    def test_import_requires_confirmation_for_manual_term_without_evidence(self) -> None:
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
        self.assertEqual(payload["code"], "GLOSSARY_MANUAL_TERMS_MISSING")
        self.assertEqual(payload["message"], "원문에서 찾을 수 없는 수동 용어가 있습니다.")
        self.assertEqual(payload["missing_terms"], ["Missing Manual Term"])
        self.assertIn("Missing Manual Term", payload["detail"])
        self.assertIn("not found in the approved source", payload["detail"])

        status, confirmed, _ = self._request(
            "/api/import",
            method="POST",
            payload={
                "review_sha256": payload["document"]["review_sha256"],
                "rows": rows,
                "allow_missing_terms": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["result"]["unverified_count"], 1)

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
