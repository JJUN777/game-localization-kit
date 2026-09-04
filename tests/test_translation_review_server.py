from __future__ import annotations

import json
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from google.genai import errors as gemini_errors

from glk.application.translation_retry_job_service import _safe_retry_error
from glk.application.translation_review_service import (
    TranslationReviewConflictError,
)
from glk.application.translation_service import translate_project
from glk.application.translation_retry_service import TranslationRetryResult
from glk.application.translation_types import (
    TranslationError,
    TranslationValidationError,
)
from glk.infrastructure.gemini_common import GeminiConfigurationError
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
        timeout: float = 3,
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
            with urlopen(request, timeout=timeout) as response:
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
        self.assertIn('id="retry-job"', html)
        self.assertIn('api("/api/retry-job")', html)
        self.assertIn("오류 재번역 다시 시도", html)
        self.assertIn("확정 용어집", html)
        self.assertIn("이 블록의 적용 용어", html)
        self.assertIn("keep_rule_applied: \"원문 유지 적용\"", html)
        self.assertIn("highlightSourceTerm", html)
        self.assertIn("최종 번역 승인이 완료되었습니다", html)
        self.assertIn('id="qa-override-button"', html)
        self.assertIn("예외 승인 후 최종 승인", html)
        self.assertIn("qa_override_reason", html)
        self.assertIn("QA 오류 ${doc.summary.overridable_errors}개 예외로 승인…", html)
        self.assertIn('class="filter-count"', html)
        self.assertIn('id="more-menu"', html)
        self.assertIn('state.dirty ? "저장하고 검증" : "검증"', html)
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
        self.assertEqual(len(document["termbase"]), 3)
        self.assertEqual(
            [
                term["source_term"]
                for term in document["blocks"][1]["relevant_terms"]
            ],
            ["Hunter", "Stamina"],
        )
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

    def test_can_finalize_with_an_audited_semantic_qa_override(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        translations[document["blocks"][0]["id"]] = "전투 3"

        status, qa_payload, _ = self._request(
            "/api/qa",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "translations": translations,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(qa_payload["result"]["passed"])
        summary = qa_payload["document"]["summary"]
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["overridable_errors"], 1)
        self.assertEqual(summary["blocking_errors"], 0)

        status, blocked, _ = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": qa_payload["document"]["review_sha256"],
                "translations": translations,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(blocked["result"]["finalized"])

        reason = "원문 월 이름을 한국어 숫자 월 표기로 옮긴 것을 확인함"
        status, finalized, _ = self._request(
            "/api/finalize",
            method="POST",
            timeout=15,
            payload={
                "review_sha256": blocked["document"]["review_sha256"],
                "translations": translations,
                "qa_override_reason": reason,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(finalized["ok"])
        self.assertTrue(finalized["result"]["finalized"])
        self.assertTrue(finalized["result"]["qa_errors_overridden"])
        self.assertEqual(finalized["result"]["error_count"], 1)
        state = json.loads(
            (self.project_path / ".glk/state/translation_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["qa_override"]["reason"], reason)
        self.assertEqual(state["qa_override"]["error_count"], 1)
        self.assertEqual(
            state["qa_override"]["review_sha256"],
            finalized["document"]["review_sha256"],
        )

    def test_can_override_protected_content_errors(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        translations[document["blocks"][2]["id"]] = (
            "사냥꾼들은 체력 10을 사용할 수 있습니다."
        )

        status, payload, _ = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "translations": translations,
                "qa_override_reason": "검토함",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["finalized"])
        self.assertTrue(payload["result"]["qa_errors_overridden"])
        self.assertTrue(
            (self.project_path / "05_output/rulebook_kor.txt").is_file()
        )

    def test_does_not_override_an_empty_translation(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        translations[document["blocks"][0]["id"]] = ""

        status, payload, _ = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "translations": translations,
                "qa_override_reason": "빈 제목을 의도함",
            },
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Empty translations cannot be overridden", payload["detail"])
        self.assertFalse(
            (self.project_path / "05_output/rulebook_kor.txt").exists()
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
        self.assertEqual(
            payload["code"],
            "TRANSLATION_REVIEW_BLOCK_MISMATCH",
        )
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
            usage={
                "model": "test-model",
                "requests": 1,
                "input_tokens": 800,
                "output_tokens": 200,
                "estimated_cost_usd": 0.002,
            },
        )
        started = threading.Event()
        release = threading.Event()

        def run_retry(**kwargs: object) -> TranslationRetryResult:
            progress = kwargs["progress"]
            self.assertTrue(callable(progress))
            progress("오류 블록 1/1 재번역 중: block-2")
            started.set()
            self.assertTrue(release.wait(2))
            return result

        with patch(
            "glk.application.translation_retry_job_service.retry_failed_translations",
            side_effect=run_retry,
        ) as retry:
            status, payload, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": document["review_sha256"],
                    "translations": translations,
                },
            )
            self.assertEqual(status, 202)
            self.assertEqual(payload["job"]["status"], "queued")
            self.assertTrue(started.wait(1))

            status, running, _ = self._request("/api/retry-job")
            self.assertEqual(status, 200)
            self.assertEqual(running["job"]["status"], "running")
            self.assertEqual(running["job"]["progress_current"], 0)
            self.assertEqual(running["job"]["progress_total"], 1)

            status, responsive, _ = self._request("/api/review")
            self.assertEqual(status, 200)
            self.assertIn("summary", responsive)
            status, conflict, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": responsive["review_sha256"],
                    "translations": {
                        block["id"]: block["translation"]
                        for block in responsive["blocks"]
                    },
                },
            )
            self.assertEqual(status, 409)
            self.assertIn("이미 진행 중", conflict["detail"])
            status, save_conflict, _ = self._request(
                "/api/save",
                method="POST",
                payload={
                    "review_sha256": responsive["review_sha256"],
                    "translations": {
                        block["id"]: block["translation"]
                        for block in responsive["blocks"]
                    },
                },
            )
            self.assertEqual(status, 409)
            self.assertIn("변경할 수 없습니다", save_conflict["detail"])

            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                _, completed, _ = self._request("/api/retry-job")
                if completed["job"]["status"] == "succeeded":
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("translation retry job did not finish")

        self.assertEqual(completed["job"]["result"]["retried_blocks"], 1)
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
        ledger_events = [
            json.loads(line)
            for line in (self.project_path / ".glk/state/ai_usage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(ledger_events[-1]["stage"], "translation_review")
        self.assertEqual(ledger_events[-1]["usage"]["requests"], 1)

    def test_failed_retry_job_exposes_reason_and_can_be_retried(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        result = TranslationRetryResult(
            project_path=str(self.project_path),
            model="test-model",
            requested_blocks=0,
            retried_blocks=0,
            block_ids=(),
            previous_error_count=0,
            remaining_error_count=0,
            warning_count=0,
            review_file=str(self.project_path / "04_translation/review.txt"),
            revision_file=None,
        )
        calls = 0

        def run_retry(**kwargs: object) -> TranslationRetryResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(
                    "/Users/private/project/review.txt: temporary provider failure"
                )
            return result

        with patch(
            "glk.application.translation_retry_job_service.retry_failed_translations",
            side_effect=run_retry,
        ):
            status, first, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": document["review_sha256"],
                    "translations": translations,
                },
            )
            self.assertEqual(status, 202)
            failed = self._wait_for_retry_job("failed")
            self.assertEqual(
                failed["error"],
                (
                    "오류 문장 재번역에 실패했습니다. "
                    "검수 내용은 유지되었습니다. 다시 시도하세요."
                ),
            )
            self.assertNotIn("/Users/private", failed["error"])

            _, latest_document, _ = self._request("/api/review")
            status, second, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": latest_document["review_sha256"],
                    "translations": {
                        block["id"]: block["translation"]
                        for block in latest_document["blocks"]
                    },
                },
            )
            self.assertEqual(status, 202)
            self.assertNotEqual(
                first["job"]["job_id"],
                second["job"]["job_id"],
            )
            self._wait_for_retry_job("succeeded")

    def test_retry_job_sanitizes_gemini_api_details(self) -> None:
        _, document, _ = self._request("/api/review")
        translations = {
            block["id"]: block["translation"]
            for block in document["blocks"]
        }
        api_error = gemini_errors.APIError(
            404,
            {
                "error": {
                    "code": 404,
                    "status": "NOT_FOUND",
                    "message": (
                        "secret SDK detail /Users/private/models/missing"
                    ),
                }
            },
            SimpleNamespace(headers={}),  # type: ignore[arg-type]
        )

        with patch(
            "glk.application.translation_retry_job_service.retry_failed_translations",
            side_effect=api_error,
        ):
            status, _, _ = self._request(
                "/api/retry",
                method="POST",
                payload={
                    "review_sha256": document["review_sha256"],
                    "translations": translations,
                },
            )
            self.assertEqual(status, 202)
            failed = self._wait_for_retry_job("failed")

        self.assertEqual(
            failed["error"],
            (
                "선택한 Gemini 모델을 사용할 수 없습니다. "
                "AI 설정에서 모델을 확인하세요."
            ),
        )
        self.assertNotIn("secret SDK detail", failed["error"])
        self.assertNotIn("/Users/private", failed["error"])

    def test_retry_error_keeps_conflict_and_validation_guidance(self) -> None:
        conflict = _safe_retry_error(
            TranslationReviewConflictError(
                "sensitive changed review detail /Users/private"
            )
        )
        validation = _safe_retry_error(
            TranslationValidationError(
                "sensitive validation detail /Users/private"
            )
        )

        self.assertIn("최신 내용을 불러온 뒤", conflict)
        self.assertIn("직접 수정하거나 다시 시도", validation)
        self.assertNotIn("/Users/private", conflict)
        self.assertNotIn("/Users/private", validation)
        self.assertEqual(
            TranslationReviewConflictError.code,
            "REVIEW_CONFLICT",
        )
        self.assertEqual(
            TranslationValidationError.code,
            "TRANSLATION_VALIDATION_FAILED",
        )

    def test_retry_error_classifies_provider_failures_without_raw_detail(
        self,
    ) -> None:
        expected_markers = {
            400: "API 키 또는 요청 설정",
            403: "호출 권한",
            404: "모델",
            429: "사용량 한도",
            500: "일시적으로 응답하지 않습니다",
        }
        for code, marker in expected_markers.items():
            with self.subTest(code=code):
                api_error = gemini_errors.APIError(
                    code,
                    {
                        "error": {
                            "code": code,
                            "status": "TEST_FAILURE",
                            "message": (
                                "secret SDK detail /Users/private/api-key"
                            ),
                        }
                    },
                    SimpleNamespace(headers={}),  # type: ignore[arg-type]
                )
                wrapped = TranslationError(
                    "Selective retry failed. Cause: secret SDK detail"
                )
                wrapped.__cause__ = api_error

                message = _safe_retry_error(wrapped)

                self.assertIn(marker, message)
                self.assertNotIn("secret SDK detail", message)
                self.assertNotIn("/Users/private", message)

        configuration = _safe_retry_error(
            GeminiConfigurationError("GEMINI_API_KEY=/Users/private/key")
        )
        network = _safe_retry_error(
            TimeoutError("socket timeout /Users/private")
        )
        self.assertIn("API 키가 설정되지 않았습니다", configuration)
        self.assertIn("네트워크 연결", network)
        self.assertNotIn("/Users/private", configuration)
        self.assertNotIn("/Users/private", network)

    def _wait_for_retry_job(self, status: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, payload, _ = self._request("/api/retry-job")
            job = payload["job"]
            if job["status"] == status:
                return job
            threading.Event().wait(0.01)
        self.fail(f"translation retry job did not reach {status}")

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

    def test_passes_settings_root_to_retry_job_manager(self) -> None:
        settings_root = Path(self.temporary_directory.name) / "settings"
        server = create_translation_review_server(
            project="translation_project",
            workspace_root=self.workspace_root,
            settings_root=settings_root,
        )
        try:
            self.assertEqual(
                server.retry_jobs.settings_root,
                settings_root.resolve(),
            )
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
