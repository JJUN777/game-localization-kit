from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from glk.application.translation_service import translate_project
from glk.application.translation_retry_service import TranslationRetryResult
from glk.infrastructure.translation_review_server import (
    TranslationReviewHttpServer,
    create_translation_review_server,
)
from tests.test_translation_service import (
    SequenceProvider,
    create_translation_project,
    sample_blocks,
    valid_response,
)


class TranslationReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = (
            Path(self.temporary_directory.name) / "workspaces"
        )
        blocks = sample_blocks()
        self.project_path = create_translation_project(
            self.workspace_root, blocks
        )
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        self.server: TranslationReviewHttpServer = (
            create_translation_review_server(
                project="translation_project",
                workspace_root=self.workspace_root,
            )
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

    def test_serves_packaged_ui_and_rejects_unauthorized_api(self) -> None:
        status, html, headers = self._request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIsInstance(html, str)
        self.assertIn("원문과 번역을 함께 검수하세요", html)
        self.assertIn("오류만 재번역", html)
        self.assertIn("최종 번역 승인이 완료되었습니다", html)
        self.assertIn('number_changed: "숫자 불일치"', html)
        self.assertIn("${issueLabel(issue)} · ${issue.message}", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)
        self.assertNotIn("__GLK_RETURN_URL_JSON__", html)
        self.assertIn("const RETURN_URL = null;", html)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

        status, payload, _ = self._request(
            "/api/review",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

        status, payload, _ = self._request(
            "/api/review",
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_save_qa_finalize_and_optimistic_conflict(self) -> None:
        status, document, _ = self._request("/api/review")
        self.assertEqual(status, 200)
        self.assertEqual(document["summary"]["blocks"], 3)
        original_hash = document["review_sha256"]
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        first_id = document["blocks"][0]["id"]
        translations[first_id] = "전투 단계"

        status, saved, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": original_hash,
                "translations": translations,
            },
        )
        self.assertEqual(status, 200)
        saved_document = saved["document"]
        self.assertNotEqual(saved_document["review_sha256"], original_hash)
        self.assertEqual(saved_document["summary"]["changed"], 1)
        review_text = (
            self.project_path / "04_translation/review.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("[ORIGINAL]\nCombat", review_text)
        self.assertIn("[TRANSLATION]\n전투 단계", review_text)

        status, conflict, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": original_hash,
                "translations": translations,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["code"], "REVIEW_CONFLICT")
        self.assertIn("새로고침", conflict["message"])
        self.assertIn("changed after", conflict["detail"])

        status, qa_payload, _ = self._request(
            "/api/qa",
            method="POST",
            payload={
                "review_sha256": saved_document["review_sha256"],
                "translations": translations,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(qa_payload["result"]["passed"])
        self.assertEqual(
            qa_payload["document"]["review_status"],
            "qa_passed",
        )

        status, final_payload, _ = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": qa_payload["document"]["review_sha256"],
                "translations": translations,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(final_payload["result"]["finalized"])
        self.assertTrue(
            final_payload["document"]["final_translation_approved"]
        )
        self.assertTrue(
            (self.project_path / "05_output/rulebook_kor.txt").is_file()
        )

    def test_rejects_unknown_block_without_modifying_review(self) -> None:
        _, document, _ = self._request("/api/review")
        before = (self.project_path / "04_translation/review.txt").read_bytes()
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        translations["unknown-block"] = "잘못된 입력"
        status, payload, _ = self._request(
            "/api/save",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "translations": translations,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "INVALID_REQUEST")
        self.assertIn("알 수 없는 블록", payload["message"])
        self.assertIn("unknown", payload["detail"])
        self.assertEqual(
            (self.project_path / "04_translation/review.txt").read_bytes(),
            before,
        )

    def test_retry_api_saves_edits_then_calls_selective_retry(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        translations[document["blocks"][1]["id"]] = (
            "각 사냥꾼은 스태미나 3을 얻습니다."
        )
        result = TranslationRetryResult(
            project_path=str(self.project_path),
            model="test-model",
            requested_blocks=1,
            retried_blocks=1,
            block_ids=(document["blocks"][1]["id"],),
            previous_error_count=1,
            remaining_error_count=0,
            warning_count=0,
            review_file=str(self.project_path / "04_translation/review.txt"),
            revision_file=str(self.project_path / "04_translation/revisions/retry.json"),
        )
        with patch(
            "glk.infrastructure.translation_review_server.retry_failed_translations",
            return_value=result,
        ) as retry:
            status, payload, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": document["review_sha256"],
                    "translations": translations,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["retried_blocks"], 1)
        self.assertEqual(
            retry.call_args.kwargs["expected_review_sha256"],
            payload["document"]["review_sha256"],
        )
        self.assertIn(
            "스태미나 3",
            (self.project_path / "04_translation/review.txt").read_text(
                encoding="utf-8"
            ),
        )

    def test_injects_only_a_local_return_url(self) -> None:
        return_url = "http://127.0.0.1:8765/"
        server = create_translation_review_server(
            project="translation_project",
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
            create_translation_review_server(
                project="translation_project",
                workspace_root=self.workspace_root,
                return_url="https://attacker.example/",
            )


if __name__ == "__main__":
    unittest.main()
