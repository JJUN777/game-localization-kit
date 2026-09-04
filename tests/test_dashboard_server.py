from __future__ import annotations

from dataclasses import replace
import errno
from io import BytesIO
from http import HTTPStatus
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from PIL import Image

from glk.application.glossary_service import build_project_glossary_candidates
from glk.application.project_service import create_project
from glk.application.source_registration_service import register_project_pdf
from glk.application.source_review_service import prepare_project_source_review
from glk.application.translation_review_service import (
    finalize_project_translation_review,
)
from glk.application.translation_prompt_service import (
    update_project_translation_prompt,
)
from glk.application.translation_service import translate_project
from glk.infrastructure.dashboard_server import (
    _MAX_UPLOAD_BYTES,
    DashboardHttpServer,
    create_dashboard_server,
)
from tests.test_glossary_service import create_approved_project, sample_blocks
from tests.test_source_review_service import make_block, write_blocks
from tests.test_translation_service import (
    SequenceProvider,
    create_translation_project,
    make_block as make_translation_block,
    sample_blocks as translation_sample_blocks,
    valid_response,
)
from tests.test_translation_prompt_ai_service import (
    FakeTranslationPromptDraftProvider,
)


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        self.settings_root = Path(self.temporary_directory.name) / "settings"
        self.environment_patch = patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "", "GEMINI_MODEL": ""},
        )
        self.environment_patch.start()
        self.source_job_calls: list[tuple[str, Path, str]] = []
        self.glossary_job_calls: list[tuple[str, Path]] = []
        self.translation_job_calls: list[
            tuple[str, Path, str, bool, bool]
        ] = []
        self.translation_prompt_draft_provider = (
            FakeTranslationPromptDraftProvider()
        )
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
            settings_root=self.settings_root,
            source_job_runner=self._run_source_job,
            glossary_job_runner=self._run_glossary_job,
            translation_job_runner=self._run_translation_job,
            translation_prompt_draft_provider=(
                self.translation_prompt_draft_provider
            ),
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
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    def _run_source_job(
        self,
        project_id: str,
        workspace_root: str | Path,
        model: str,
        progress: object,
    ) -> dict[str, object]:
        self.source_job_calls.append(
            (project_id, Path(workspace_root), model)
        )
        progress("Page 1: requesting LLM layout reconstruction", 0, 1)  # type: ignore[operator]
        return {
            "ok": True,
            "status": "succeeded",
            "source_type": "pdf",
        }

    def test_uses_one_settings_root_for_status_and_background_jobs(
        self,
    ) -> None:
        expected = self.settings_root.resolve()

        self.assertEqual(self.server.settings_root, expected)
        self.assertEqual(self.server.ai_settings.settings_root, expected)
        self.assertEqual(self.server.job_manager.settings_root, expected)

    def test_bind_failure_preserves_original_socket_error(self) -> None:
        bind_error = OSError(errno.EADDRINUSE, "Address already in use")
        with (
            patch(
                "glk.infrastructure.local_http.TCPServer.server_bind",
                side_effect=bind_error,
            ),
            self.assertRaises(OSError) as raised,
        ):
            create_dashboard_server(
                workspace_root=self.workspace_root,
                settings_root=self.settings_root,
            )

        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)

    def _run_glossary_job(
        self,
        project_id: str,
        workspace_root: str | Path,
        progress: object,
    ) -> dict[str, object]:
        self.glossary_job_calls.append(
            (project_id, Path(workspace_root))
        )
        progress("용어 후보를 생성하고 있습니다.", 1, 2)  # type: ignore[operator]
        return {
            "ok": True,
            "status": "succeeded",
            "glossary": {"candidate_count": 4},
        }

    def _run_translation_job(
        self,
        project_id: str,
        workspace_root: str | Path,
        model: str,
        resume: bool,
        force: bool,
        progress: object,
    ) -> dict[str, object]:
        self.translation_job_calls.append(
            (
                project_id,
                Path(workspace_root),
                model,
                resume,
                force,
            )
        )
        progress("Chunk 1/1: requesting translation", 0, 1)  # type: ignore[operator]
        return {
            "ok": True,
            "status": "succeeded",
            "translation": {
                "completed_blocks": 3,
                "completed_chunks": 1,
            },
        }

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        authorized: bool = True,
        origin: str | None = None,
    ) -> tuple[int, dict[str, object] | str]:
        headers: dict[str, str] = {}
        data = None
        if authorized:
            headers["X-GLK-Token"] = self.server.auth_token
        if origin is not None:
            headers["Origin"] = origin
        if body is not None and payload is not None:
            raise AssertionError("Use either payload or body.")
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = body
            if content_type is not None:
                headers["Content-Type"] = content_type
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

    @staticmethod
    def _multipart_upload(
        source_type: str,
        files: list[tuple[str, bytes, str]],
        *,
        ocr_prompt: str | None = None,
    ) -> tuple[bytes, str]:
        boundary = "----glk-dashboard-test-boundary"
        parts: list[bytes] = [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="source_type"\r\n'
                "\r\n"
                f"{source_type}\r\n"
            ).encode("utf-8")
        ]
        if ocr_prompt is not None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="ocr_prompt"\r\n'
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                    f"{ocr_prompt}\r\n"
                ).encode("utf-8")
            )
        for filename, content, mime_type in files:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="files"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {mime_type}\r\n"
                    "\r\n"
                ).encode("utf-8")
                + content
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        return (
            b"".join(parts),
            f"multipart/form-data; boundary={boundary}",
        )

    def test_serves_dashboard_and_protects_api(self) -> None:
        status, html = self._request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("Game Localization Kit Dashboard", html)
        self.assertIn('/assets/dashboard.js', html)
        status, script = self._request("/assets/dashboard.js")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIsInstance(script, str)
        self.assertIn("MAX_SOURCE_UPLOAD_BYTES = 500 * 1024 * 1024", script)
        self.assertIn("function updateActiveJobDisplays()", script)
        self.assertIn("function updateActiveJobElapsed()", script)
        self.assertIn("function continuePartialSourceReview(job)", script)
        self.assertIn("/api/jobs/source/continue", script)
        self.assertIn("window.setInterval(updateActiveJobElapsed, 1000)", script)
        self.assertIn("누적 AI 비용", script)
        self.assertIn("pipelineTotal(project)", script)
        self.assertIn('pipelineStep(project, "translation_review"', script)
        self.assertIn('class="pipeline-step ${state}"', script)
        self.assertIn('class="pipeline-step-label pipeline-step-link"', script)
        self.assertIn('class="pipeline-step-cost"${detail}', script)
        self.assertIn('data-review="${reviewType}"', script)
        self.assertIn("function primaryReviewButton(project)", script)
        self.assertIn("function primaryActionButton(project)", script)
        self.assertIn("function projectActionRow(project)", script)
        self.assertIn("function projectFiles(project)", script)
        self.assertIn("승인 원문", script)
        self.assertNotIn("원문 TXT 저장", script)
        self.assertIn('actionMenu("설정"', script)
        self.assertIn('summary>작업 기록 ${completed.length}건', script)
        self.assertIn('class="translation-prompt-button contextual-action"', script)
        self.assertNotIn("다른 백그라운드 작업 실행 중", script)
        self.assertIn("data-usage-detail", script)
        self.assertIn('data-project-id="${escapeHtml(project.project_id)}"', script)
        self.assertIn(
            'message.textContent = job.progress_message || ""',
            script,
        )
        self.assertIn("updateActiveJobDisplays();", script)
        status, tokens = self._request("/assets/tokens.css")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn("prefers-color-scheme: dark", tokens)
        self.assertIn("--on-solid: #0f1216;", tokens)
        html = f"{html}\n{script}"
        self.assertIn("선택 파일 한도는 500 MiB", html)
        self.assertIn("data-create-project", html)
        self.assertIn("data-delete-project", html)
        self.assertIn("원본 교체", html)
        self.assertIn("원본 파일 목록", html)
        self.assertIn("OCR 프롬프트", html)
        self.assertIn("OCR 프롬프트 수정", html)
        self.assertNotIn('id="ocrPromptField"', html)
        self.assertNotIn('id="ocrPrompt"', html)
        self.assertIn("프로젝트 기본 OCR 프롬프트를 유지하며", html)
        self.assertIn("저장된 OCR 프롬프트는 유지되며", html)
        self.assertIn("편집 전 내용으로 되돌리기", html)
        self.assertIn("AI 설정", html)
        self.assertIn("원문 준비 시작", html)
        self.assertIn('id="sourceJobOcrPromptField"', html)
        self.assertIn('id="sourceJobOcrPrompt" readonly', html)
        self.assertIn(
            'showsOcrPrompt ? project.ocr_prompt || "" : ""',
            html,
        )
        self.assertIn("이번 작업에 적용할 OCR 프롬프트", html)
        self.assertIn("용어 후보 생성", html)
        self.assertEqual(_MAX_UPLOAD_BYTES, 512 * 1024 * 1024)
        self.assertIn("AI API를 사용하지 않으며", html)
        self.assertIn("초벌 번역 시작", html)
        self.assertIn("번역 문체·표현 지침", html)
        self.assertIn("번역 프롬프트 설정", html)
        self.assertIn("AI로 초안 만들기", html)
        self.assertIn("translationPromptEditorView", html)
        self.assertIn("translationPromptAiPanel", html)
        self.assertIn("translationPromptAiNew", html)
        self.assertIn("AI 번역 프롬프트 초안", html)
        self.assertIn("저장된 AI 초안", html)
        self.assertIn("승인한 원문의 앞부분과 일부 규칙 문장", html)
        self.assertIn("프롬프트 저장</strong>을 눌러야", html)
        self.assertIn("showTranslationPromptAiView()", html)
        self.assertIn("showTranslationPromptEditorView(true)", html)
        self.assertIn("translation-prompt-ai-estimate", html)
        self.assertIn("translation-prompt-ai-draft", html)
        self.assertIn("이 초안 사용", html)
        self.assertIn("돌아가기", html)
        self.assertIn("AI 초안이 입력되었습니다", html)
        self.assertIn("입력 약", html)
        self.assertIn("usage.input_tokens", html)
        self.assertIn("usage.output_tokens", html)
        self.assertIn("data-edit-translation-prompt", html)
        self.assertIn(
            'project.pipeline.termbase_status !== "current"',
            html,
        )
        self.assertIn("변경된 프롬프트로 전체 재번역", html)
        self.assertIn("청크마다 API를 호출", html)
        self.assertIn("초벌 번역 완료 ·", html)
        self.assertIn("translation-review-attention", html)
        self.assertIn("파일 저장", html)
        self.assertIn("최종 번역본", html)
        self.assertIn("승인 원문", html)
        self.assertIn("data-download-source", html)
        self.assertIn("data-download-output", html)
        self.assertIn("data-download-previous-output", html)
        self.assertIn("data-download-output-archive", html)
        self.assertIn("통합 번역본", html)
        self.assertIn("이미지별 번역본", html)
        self.assertIn('id="toast" role="status"', html)
        self.assertIn('document.querySelectorAll("dialog[open]")', html)
        self.assertIn("toastHost.append(toast)", html)
        self.assertIn("/api/output-archive", html)
        self.assertIn("/api/previous-output", html)
        self.assertIn("/api/source-output", html)
        self.assertIn("window.showSaveFilePicker", html)
        self.assertIn("fileHandle.createWritable()", html)
        self.assertIn('error?.name === "AbortError"', html)
        self.assertIn("저장 위치를 선택해 다운로드", html)
        self.assertIn('data-replace-source="true"', html)
        self.assertIn('"source-replace-submit"', html)
        self.assertIn("휴지통으로 이동", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)

        project_card = html.split(
            "function projectCard(project)",
            maxsplit=1,
        )[1].split("function createProjectCard()", maxsplit=1)[0]
        self.assertIn("${projectActionRow(project)}", project_card)
        self.assertIn("${projectFiles(project)}", project_card)
        self.assertNotIn('${reviewButton(project, "source")}', project_card)
        self.assertNotIn('${reviewButton(project, "glossary")}', project_card)
        self.assertNotIn('${reviewButton(project, "translation")}', project_card)

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

    def test_estimates_and_generates_translation_prompt_ai_draft(self) -> None:
        project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        current_prompt = "자연스러운 한국어 규칙서 문체로 번역하세요."

        status, estimate = self._request(
            "/api/projects/glossary_project/translation-prompt-ai-estimate",
            method="POST",
            payload={"current_prompt": current_prompt},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(estimate["request_count"], 1)
        self.assertGreater(estimate["estimated_input_tokens"], 0)
        self.assertGreater(estimate["estimated_output_tokens_high"], 0)

        status, result = self._request(
            "/api/projects/glossary_project/translation-prompt-ai-draft",
            method="POST",
            payload={"current_prompt": current_prompt},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(result["cached"])
        self.assertEqual(result["usage"]["requests"], 1)
        self.assertEqual(result["usage"]["input_tokens"], 320)
        self.assertEqual(result["usage"]["output_tokens"], 140)
        self.assertFalse(
            (project_path / "04_translation/prompt.txt").is_file()
        )

        status, cached = self._request(
            "/api/projects/glossary_project/translation-prompt-ai-estimate",
            method="POST",
            payload={"current_prompt": current_prompt},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(cached["cached"])
        self.assertIn("draft", cached["cached_result"])
        self.assertEqual(cached["request_count"], 0)
        self.assertEqual(cached["estimated_input_tokens"], 0)

        status, forced_estimate = self._request(
            "/api/projects/glossary_project/translation-prompt-ai-estimate",
            method="POST",
            payload={"current_prompt": current_prompt, "force": True},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(forced_estimate["cached"])
        self.assertEqual(forced_estimate["request_count"], 1)

        status, forced = self._request(
            "/api/projects/glossary_project/translation-prompt-ai-draft",
            method="POST",
            payload={"current_prompt": current_prompt, "force": True},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(forced["cached"])
        self.assertEqual(
            len(self.translation_prompt_draft_provider.prompts),
            2,
        )

    def test_reads_and_updates_ai_settings_without_returning_the_key(
        self,
    ) -> None:
        status, unauthorized = self._request(
            "/api/settings/ai",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(unauthorized["code"], "REVIEW_SESSION_INVALID")

        status, initial = self._request("/api/settings/ai")
        self.assertEqual(status, 200)
        self.assertFalse(initial["settings"]["api_key_configured"])
        self.assertEqual(initial["settings"]["model"], "gemini-3.8-flash")
        self.assertNotIn("api_key", initial["settings"])
        self.assertEqual(
            [
                model["id"]
                for model in initial["model_catalog"]["models"]
            ],
            [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
            ],
        )
        self.assertEqual(
            initial["model_catalog"]["source_url"],
            "https://ai.google.dev/gemini-api/docs/models",
        )
        self.assertEqual(
            initial["model_catalogs"]["openai"]["provider"],
            "openai",
        )

        status, saved = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={
                "api_key": "dashboard-secret-key",
                "model": "gemini-3.6-flash",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["settings"]["api_key_configured"])
        self.assertEqual(saved["settings"]["model"], "gemini-3.6-flash")
        self.assertNotIn("dashboard-secret-key", json.dumps(saved))

        env_path = self.settings_root / ".env"
        env_text = env_path.read_text(encoding="utf-8")
        self.assertIn('GEMINI_API_KEY="dashboard-secret-key"', env_text)
        self.assertIn('GEMINI_MODEL="gemini-3.6-flash"', env_text)

        status, changed_model = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={"api_key": "", "model": "custom-model-v2"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            changed_model["settings"]["model"],
            "custom-model-v2",
        )
        self.assertIn(
            'GEMINI_API_KEY="dashboard-secret-key"',
            env_path.read_text(encoding="utf-8"),
        )

        status, openai_saved = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={
                "provider": "openai",
                "api_key": "sk-dashboard-openai",
                "model": "gpt-5.6-terra",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(openai_saved["settings"]["provider"], "openai")
        self.assertEqual(openai_saved["settings"]["model"], "gpt-5.6-terra")
        self.assertNotIn("sk-dashboard-openai", json.dumps(openai_saved))
        env_text = env_path.read_text(encoding="utf-8")
        self.assertIn('GLK_AI_PROVIDER="openai"', env_text)
        self.assertIn('OPENAI_API_KEY="sk-dashboard-openai"', env_text)
        self.assertIn('OPENAI_MODEL="gpt-5.6-terra"', env_text)

        status, invalid = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={"api_key": "", "model": "invalid model"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "AI_SETTINGS_UPDATE_FAILED")

    def test_starts_and_reports_a_background_source_job(self) -> None:
        source_pdf = Path(self.temporary_directory.name) / "job.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\njob source\n")
        create_project(
            name="Job Project",
            project_id="job_project",
            workspace_root=self.workspace_root,
        )
        register_project_pdf(
            project="job_project",
            file=source_pdf,
            workspace_root=self.workspace_root,
        )

        status, unauthorized = self._request(
            "/api/jobs",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(unauthorized["code"], "REVIEW_SESSION_INVALID")

        status, missing_key = self._request(
            "/api/jobs/source",
            method="POST",
            payload={"project_id": "job_project"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            missing_key["code"],
            "SOURCE_JOB_START_FAILED",
        )

        status, _ = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={
                "api_key": "dashboard-job-key",
                "model": "gemini-3.5-flash",
            },
        )
        self.assertEqual(status, 200)

        status, started = self._request(
            "/api/jobs/source",
            method="POST",
            payload={"project_id": "job_project"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(started["job"]["project_id"], "job_project")
        self.assertEqual(started["job"]["model"], "gemini-3.5-flash")

        job: dict[str, object] | None = None
        for _ in range(100):
            status, jobs = self._request("/api/jobs")
            self.assertEqual(status, 200)
            matching = [
                value
                for value in jobs["jobs"]
                if value["project_id"] == "job_project"
            ]
            if matching and matching[0]["status"] == "succeeded":
                job = matching[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(job)
        self.assertEqual(job["progress_total"], 1)
        self.assertEqual(
            self.source_job_calls,
            [
                (
                    "job_project",
                    self.workspace_root.resolve(),
                    "gemini-3.5-flash",
                )
            ],
        )

    def test_starts_and_reports_a_background_glossary_job(self) -> None:
        status, not_approved = self._request(
            "/api/jobs/glossary",
            method="POST",
            payload={"project_id": "dashboard_review"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            not_approved["code"],
            "GLOSSARY_JOB_START_FAILED",
        )

        create_approved_project(self.workspace_root, sample_blocks())

        status, started = self._request(
            "/api/jobs/glossary",
            method="POST",
            payload={"project_id": "glossary_project"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(started["job"]["project_id"], "glossary_project")

        job: dict[str, object] | None = None
        for _ in range(100):
            status, jobs = self._request("/api/jobs")
            self.assertEqual(status, 200)
            matching = [
                value
                for value in jobs["glossary_jobs"]
                if value["project_id"] == "glossary_project"
            ]
            if matching and matching[0]["status"] == "succeeded":
                job = matching[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(job)
        self.assertEqual(job["result"]["glossary"]["candidate_count"], 4)  # type: ignore[index]
        self.assertEqual(
            self.glossary_job_calls,
            [("glossary_project", self.workspace_root.resolve())],
        )

        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        glossary_project = next(
            project
            for project in dashboard["projects"]
            if project["project_id"] == "glossary_project"
        )
        self.assertTrue(glossary_project["pipeline"]["final_source_approved"])

    def test_starts_and_reports_a_background_translation_job(self) -> None:
        create_translation_project(
            self.workspace_root,
            [
                make_translation_block(1, "COMBAT", block_type="heading"),
                make_translation_block(2, "Each Hunter gains 2 Stamina."),
                make_translation_block(3, "Hunters may spend Stamina."),
            ],
        )
        status, missing_key = self._request(
            "/api/jobs/translation",
            method="POST",
            payload={
                "project_id": "translation_project",
                "prompt": "Translate naturally.",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            missing_key["code"],
            "TRANSLATION_JOB_START_FAILED",
        )

        status, _ = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={
                "api_key": "dashboard-translation-key",
                "model": "gemini-3.5-flash",
            },
        )
        self.assertEqual(status, 200)
        status, started = self._request(
            "/api/jobs/translation",
            method="POST",
            payload={
                "project_id": "translation_project",
                "prompt": "Translate naturally.",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(started["job"]["project_id"], "translation_project")
        self.assertFalse(started["job"]["resume"])

        job: dict[str, object] | None = None
        for _ in range(100):
            status, jobs = self._request("/api/jobs")
            self.assertEqual(status, 200)
            matching = [
                value
                for value in jobs["translation_jobs"]
                if value["project_id"] == "translation_project"
            ]
            if matching and matching[0]["status"] == "succeeded":
                job = matching[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(job)
        self.assertEqual(job["progress_total"], 1)
        self.assertEqual(
            self.translation_job_calls,
            [
                (
                    "translation_project",
                    self.workspace_root.resolve(),
                    "gemini-3.5-flash",
                    False,
                    False,
                )
            ],
        )
        prompt_path = (
            self.workspace_root
            / "translation_project/04_translation/prompt.txt"
        )
        self.assertEqual(
            prompt_path.read_text(encoding="utf-8"),
            "Translate naturally.",
        )

    def test_saves_translation_prompt_and_starts_full_retranslation(self) -> None:
        blocks = translation_sample_blocks()
        project_path = create_translation_project(
            self.workspace_root,
            blocks,
        )
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        project = next(
            item
            for item in dashboard["projects"]
            if item["project_id"] == "translation_project"
        )

        status, unauthorized = self._request(
            "/api/projects/translation_project/translation-prompt",
            method="PATCH",
            payload={
                "translation_prompt": "Use a terse rulebook style.",
                "expected_sha256": project["translation_prompt"]["sha256"],
            },
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        status, updated = self._request(
            "/api/projects/translation_project/translation-prompt",
            method="PATCH",
            payload={
                "translation_prompt": "Use a terse rulebook style.",
                "expected_sha256": project["translation_prompt"]["sha256"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            updated["translation_prompt"]["translation_invalidated"]
        )
        self.assertTrue(updated["translation_prompt"]["revision_file"])
        self.assertEqual(
            (project_path / "04_translation/prompt.txt").read_text(
                encoding="utf-8"
            ),
            "Use a terse rulebook style.\n",
        )

        status, refreshed = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        project = next(
            item
            for item in refreshed["projects"]
            if item["project_id"] == "translation_project"
        )
        self.assertEqual(project["pipeline"]["translation_status"], "stale")

        status, _ = self._request(
            "/api/settings/ai",
            method="PUT",
            payload={
                "api_key": "dashboard-translation-key",
                "model": "gemini-3.5-flash",
            },
        )
        self.assertEqual(status, 200)
        status, started = self._request(
            "/api/jobs/translation",
            method="POST",
            payload={
                "project_id": "translation_project",
                "prompt": project["translation_prompt"]["value"],
                "force": True,
            },
        )
        self.assertEqual(status, 202)
        self.assertTrue(started["job"]["force"])
        for _ in range(100):
            if self.translation_job_calls:
                break
            time.sleep(0.01)
        self.assertEqual(
            self.translation_job_calls[-1],
            (
                "translation_project",
                self.workspace_root.resolve(),
                "gemini-3.5-flash",
                False,
                True,
            ),
        )

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
        self.assertIn("원문 승인이 완료되었습니다", html)
        self.assertIn("AI 정렬 누락만 보기", html)
        self.assertIn(
            f"const RETURN_URL = {json.dumps(self.server.dashboard_url)};",
            html,
        )

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

        create_approved_project(self.workspace_root, sample_blocks())
        build_project_glossary_candidates(
            project="glossary_project",
            workspace_root=self.workspace_root,
        )
        status, glossary_opened = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "glossary_project",
                "review_type": "glossary",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(glossary_opened["ok"])
        with urlopen(str(glossary_opened["url"]), timeout=3) as response:
            glossary_html = response.read().decode("utf-8")
        self.assertIn("용어집 확정 완료", glossary_html)
        self.assertIn(
            f"const RETURN_URL = {json.dumps(self.server.dashboard_url)};",
            glossary_html,
        )

        translation_blocks = translation_sample_blocks()
        create_translation_project(self.workspace_root, translation_blocks)
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(translation_blocks)]),
        )
        status, translation_opened = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "translation_project",
                "review_type": "translation",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(translation_opened["ok"])
        with urlopen(str(translation_opened["url"]), timeout=3) as response:
            translation_html = response.read().decode("utf-8")
        self.assertIn(
            "최종 번역 승인이 완료되었습니다",
            translation_html,
        )
        self.assertIn(
            f"const RETURN_URL = {json.dumps(self.server.dashboard_url)};",
            translation_html,
        )

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

        status, escaped = self._request(
            "/api/review/open",
            method="POST",
            payload={
                "project_id": "../dashboard_review",
                "review_type": "source",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(escaped["code"], "INVALID_REQUEST")

    def test_downloads_only_the_current_approved_source(self) -> None:
        project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        path = "/api/source-output?project_id=glossary_project"

        status, unauthorized = self._request(path, authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        request = Request(
            f"{self.server.origin}{path}",
            headers={"X-GLK-Token": self.server.auth_token},
        )
        with urlopen(request, timeout=3) as response:
            data = response.read()
            content_disposition = response.headers["Content-Disposition"]
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get_content_type(),
                "text/plain",
            )
        text = data.decode("utf-8")
        self.assertEqual(text.count("[PAGE 1]"), 1)
        self.assertNotIn("[BLOCK ", text)
        self.assertNotIn("[[GLK_", text)
        self.assertIn("Furwing", text)
        self.assertIn("Each Hunter gains 2 Stamina.", text)
        self.assertIn("glossary_project_source.txt", content_disposition)

        (project_path / "02_source/final.txt").write_text(
            "tampered",
            encoding="utf-8",
        )
        status, changed = self._request(path)
        self.assertEqual(status, 400)
        self.assertEqual(changed["code"], "SOURCE_OUTPUT_DOWNLOAD_FAILED")

    def test_downloads_only_a_current_approved_output(self) -> None:
        blocks = translation_sample_blocks()
        create_translation_project(self.workspace_root, blocks)
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        finalize_project_translation_review(
            project="translation_project",
            workspace_root=self.workspace_root,
        )
        query = urlencode(
            {
                "project_id": "translation_project",
                "path": "05_output/rulebook_kor.txt",
            }
        )

        status, unauthorized = self._request(
            f"/api/output?{query}",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        request = Request(
            f"{self.server.origin}/api/output?{query}",
            headers={"X-GLK-Token": self.server.auth_token},
        )
        with urlopen(request, timeout=3) as response:
            data = response.read()
            content_disposition = response.headers["Content-Disposition"]
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get_content_type(),
                "application/octet-stream",
            )
        self.assertIn("전투", data.decode("utf-8"))
        self.assertIn("rulebook_kor.txt", content_disposition)

        escaped_query = urlencode(
            {
                "project_id": "translation_project",
                "path": "../project.json",
            }
        )
        status, escaped = self._request(f"/api/output?{escaped_query}")
        self.assertEqual(status, 400)
        self.assertEqual(escaped["code"], "OUTPUT_DOWNLOAD_FAILED")

    def test_downloads_a_previous_approved_output_after_prompt_change(self) -> None:
        blocks = translation_sample_blocks()
        create_translation_project(self.workspace_root, blocks)
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        finalize_project_translation_review(
            project="translation_project",
            workspace_root=self.workspace_root,
        )
        update_project_translation_prompt(
            project="translation_project",
            translation_prompt="새 번역 방향을 적용하세요.",
            workspace_root=self.workspace_root,
        )
        query = urlencode(
            {
                "project_id": "translation_project",
                "revision": "active",
                "path": "05_output/rulebook_kor.txt",
            }
        )
        path = f"/api/previous-output?{query}"

        status, unauthorized = self._request(path, authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        request = Request(
            f"{self.server.origin}{path}",
            headers={"X-GLK-Token": self.server.auth_token},
        )
        with urlopen(request, timeout=3) as response:
            data = response.read()
            content_disposition = response.headers["Content-Disposition"]
            self.assertEqual(response.status, 200)
        self.assertIn("전투", data.decode("utf-8"))
        self.assertRegex(
            content_disposition,
            r"rulebook_kor_\d{8}_\d{6}\.txt",
        )

        escaped_query = urlencode(
            {
                "project_id": "translation_project",
                "revision": "../escape",
                "path": "05_output/rulebook_kor.txt",
            }
        )
        status, escaped = self._request(
            f"/api/previous-output?{escaped_query}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            escaped["code"],
            "PREVIOUS_OUTPUT_DOWNLOAD_FAILED",
        )

    def test_downloads_all_per_image_outputs_as_one_archive(self) -> None:
        pdf_blocks = translation_sample_blocks()
        blocks = [
            replace(
                pdf_blocks[0],
                source_type="image",
                source_file="01_input/images/cards/card-01.png",
                page=None,
            ),
            replace(
                pdf_blocks[1],
                source_type="image",
                source_file="01_input/images/cards/card-01.png",
                page=None,
            ),
            replace(
                pdf_blocks[2],
                source_type="image",
                source_file="01_input/images/boards/board-02.png",
                page=None,
            ),
        ]
        create_translation_project(self.workspace_root, blocks)
        translate_project(
            project="translation_project",
            workspace_root=self.workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        finalize_project_translation_review(
            project="translation_project",
            workspace_root=self.workspace_root,
        )
        path = "/api/output-archive?project_id=translation_project"

        status, unauthorized = self._request(path, authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        request = Request(
            f"{self.server.origin}{path}",
            headers={"X-GLK-Token": self.server.auth_token},
        )
        with urlopen(request, timeout=3) as response:
            archive_data = response.read()
            content_disposition = response.headers["Content-Disposition"]
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get_content_type(),
                "application/zip",
            )
        self.assertIn(
            "translation_project_image_outputs.zip",
            content_disposition,
        )
        with ZipFile(BytesIO(archive_data)) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "boards/board-02_kor.txt",
                    "cards/card-01_kor.txt",
                ],
            )
            self.assertIn(
                "전투",
                archive.read("cards/card-01_kor.txt").decode("utf-8"),
            )
            self.assertNotIn("combined_kor.txt", archive.namelist())

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

    def test_registers_pdf_source_without_running_extraction(self) -> None:
        create_project(
            name="Upload PDF",
            project_id="upload_pdf",
            workspace_root=self.workspace_root,
        )
        body, content_type = self._multipart_upload(
            "pdf",
            [
                (
                    "rulebook.pdf",
                    b"%PDF-1.4\nregistered from dashboard\n%%EOF\n",
                    "application/pdf",
                )
            ],
        )

        status, unauthorized = self._request(
            "/api/projects/upload_pdf/source",
            method="POST",
            body=body,
            content_type=content_type,
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        status, uploaded = self._request(
            "/api/projects/upload_pdf/source",
            method="POST",
            body=body,
            content_type=content_type,
        )

        self.assertEqual(status, 201)
        self.assertTrue(uploaded["ok"])
        self.assertEqual(uploaded["source"]["source_type"], "pdf")
        self.assertEqual(
            uploaded["source"]["files"],
            ["01_input/pdf/rulebook.pdf"],
        )
        project_path = self.workspace_root / "upload_pdf"
        self.assertTrue((project_path / "01_input/pdf/rulebook.pdf").is_file())
        self.assertFalse(
            (project_path / ".glk/state/pdf_acquisition.json").exists()
        )

        status, duplicate = self._request(
            "/api/projects/upload_pdf/source",
            method="POST",
            body=body,
            content_type=content_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(duplicate["code"], "SOURCE_REGISTER_FAILED")

    def test_rechecks_active_job_inside_project_mutation_lock(self) -> None:
        upload_location = create_project(
            name="Race Upload",
            project_id="race_upload",
            workspace_root=self.workspace_root,
        )
        upload_body, upload_type = self._multipart_upload(
            "pdf",
            [("new.pdf", b"%PDF-1.4\nnew\n", "application/pdf")],
        )
        with patch.object(
            self.server.job_manager,
            "is_project_active",
            side_effect=[False, True],
        ) as active:
            status, blocked_upload = self._request(
                "/api/projects/race_upload/source",
                method="POST",
                body=upload_body,
                content_type=upload_type,
            )
        self.assertEqual(status, 409)
        self.assertEqual(blocked_upload["code"], "SOURCE_JOB_CONFLICT")
        self.assertEqual(active.call_count, 2)
        self.assertFalse(
            (upload_location.path / "01_input/pdf/new.pdf").exists()
        )

        old_pdf = Path(self.temporary_directory.name) / "old-race.pdf"
        old_pdf.write_bytes(b"%PDF-1.4\nold\n")
        register_project_pdf(
            project="race_upload",
            file=old_pdf,
            workspace_root=self.workspace_root,
        )
        replacement_body, replacement_type = self._multipart_upload(
            "pdf",
            [("replacement.pdf", b"%PDF-1.4\nreplacement\n", "application/pdf")],
        )
        with patch.object(
            self.server.job_manager,
            "is_project_active",
            side_effect=[False, True],
        ) as active:
            status, blocked_replace = self._request(
                "/api/projects/race_upload/source",
                method="PUT",
                body=replacement_body,
                content_type=replacement_type,
            )
        self.assertEqual(status, 409)
        self.assertEqual(blocked_replace["code"], "SOURCE_JOB_CONFLICT")
        self.assertEqual(active.call_count, 2)
        self.assertTrue(
            (upload_location.path / "01_input/pdf/old-race.pdf").is_file()
        )
        self.assertFalse(
            (
                upload_location.path
                / "01_input/pdf/replacement.pdf"
            ).exists()
        )

        prompt_location = create_project(
            name="Race Prompt",
            project_id="race_prompt",
            workspace_root=self.workspace_root,
        )
        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        prompt_project = next(
            item
            for item in dashboard["projects"]
            if item["project_id"] == "race_prompt"
        )
        prompt_path = prompt_location.path / "04_translation/prompt.txt"
        prompt_before = (
            prompt_path.read_bytes() if prompt_path.is_file() else None
        )
        with patch.object(
            self.server.job_manager,
            "is_project_active",
            side_effect=[False, True],
        ) as active:
            status, blocked_prompt = self._request(
                "/api/projects/race_prompt/translation-prompt",
                method="PATCH",
                payload={
                    "translation_prompt": "Must not be saved.",
                    "expected_sha256": (
                        prompt_project["translation_prompt"]["sha256"]
                    ),
                },
            )
        self.assertEqual(status, 409)
        self.assertEqual(
            blocked_prompt["code"],
            "TRANSLATION_JOB_CONFLICT",
        )
        self.assertEqual(active.call_count, 2)
        self.assertEqual(
            prompt_path.read_bytes() if prompt_path.is_file() else None,
            prompt_before,
        )

        delete_location = create_project(
            name="Race Delete",
            project_id="race_delete",
            workspace_root=self.workspace_root,
        )
        with (
            patch.object(
                self.server.job_manager,
                "is_project_active",
                side_effect=[False, True],
            ) as active,
            patch(
                "glk.infrastructure.dashboard_server.send2trash"
            ) as mocked_send2trash,
        ):
            status, blocked_delete = self._request(
                "/api/projects/race_delete",
                method="DELETE",
            )
        self.assertEqual(status, 409)
        self.assertEqual(blocked_delete["code"], "SOURCE_JOB_CONFLICT")
        self.assertEqual(active.call_count, 2)
        mocked_send2trash.assert_not_called()
        self.assertTrue(delete_location.path.is_dir())

    def test_failed_source_restore_reports_preserved_backup_path(self) -> None:
        location = create_project(
            name="Restore Failure",
            project_id="restore_failure",
            workspace_root=self.workspace_root,
        )
        old_pdf = Path(self.temporary_directory.name) / "restore-old.pdf"
        old_pdf.write_bytes(b"%PDF-1.4\nold\n")
        register_project_pdf(
            project="restore_failure",
            file=old_pdf,
            workspace_root=self.workspace_root,
        )
        body, content_type = self._multipart_upload(
            "pdf",
            [("new.pdf", b"%PDF-1.4\nnew\n", "application/pdf")],
        )

        with (
            patch(
                "glk.application.source_registration_service."
                "copy_file_atomic",
                side_effect=OSError("replacement copy failed"),
            ),
            patch(
                "glk.application.source_registration_service."
                "shutil.copytree",
                side_effect=OSError("restore copy failed"),
            ),
        ):
            status, failed = self._request(
                "/api/projects/restore_failure/source",
                method="PUT",
                body=body,
                content_type=content_type,
            )

        self.assertEqual(status, 500)
        self.assertEqual(failed["code"], "SOURCE_REPLACE_FAILED")
        self.assertIn("백업 보존 위치", failed["message"])
        backups = list(
            (location.path / ".glk").glob("source-replacement-*")
        )
        self.assertEqual(len(backups), 1)
        self.assertTrue(
            (backups[0] / "pdf/restore-old.pdf").is_file()
        )

    def test_registers_images_and_preserves_default_ocr_prompt(self) -> None:
        location = create_project(
            name="Upload Images",
            project_id="upload_images",
            workspace_root=self.workspace_root,
        )
        prompt_path = location.path / "01_input/images/ocr_prompt.txt"
        prompt_before = prompt_path.read_bytes()

        def png_bytes(color: str) -> bytes:
            output = BytesIO()
            Image.new("RGB", (8, 8), color).save(output, format="PNG")
            return output.getvalue()

        body, content_type = self._multipart_upload(
            "images",
            [
                ("card-10.png", png_bytes("white"), "image/png"),
                ("card-2.png", png_bytes("black"), "image/png"),
            ],
        )
        status, uploaded = self._request(
            "/api/projects/upload_images/source",
            method="POST",
            body=body,
            content_type=content_type,
        )

        self.assertEqual(status, 201)
        self.assertEqual(
            uploaded["source"]["files"],
            [
                "01_input/images/card-2.png",
                "01_input/images/card-10.png",
            ],
        )
        self.assertFalse(uploaded["source"]["ocr_prompt_updated"])
        project_path = self.workspace_root / "upload_images"
        self.assertTrue(
            (project_path / "01_input/images/card-2.png").is_file()
        )
        self.assertFalse(
            (project_path / ".glk/state/image_ocr.json").exists()
        )
        self.assertEqual(prompt_path.read_bytes(), prompt_before)

    def test_rejects_invalid_ocr_prompt_uploads(self) -> None:
        create_project(
            name="Invalid OCR Prompt",
            project_id="invalid_ocr_prompt",
            workspace_root=self.workspace_root,
        )
        image_output = BytesIO()
        Image.new("RGB", (8, 8), "white").save(
            image_output,
            format="PNG",
        )
        image_file = [
            ("card.png", image_output.getvalue(), "image/png"),
        ]

        empty_body, empty_type = self._multipart_upload(
            "images",
            image_file,
            ocr_prompt="   ",
        )
        status, empty = self._request(
            "/api/projects/invalid_ocr_prompt/source",
            method="POST",
            body=empty_body,
            content_type=empty_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(empty["code"], "SOURCE_REGISTER_FAILED")
        self.assertEqual(
            empty["message"],
            "이미지 OCR 프롬프트를 입력하세요.",
        )

        large_body, large_type = self._multipart_upload(
            "images",
            image_file,
            ocr_prompt="가" * 22_000,
        )
        status, large = self._request(
            "/api/projects/invalid_ocr_prompt/source",
            method="POST",
            body=large_body,
            content_type=large_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(large["code"], "SOURCE_REGISTER_FAILED")
        self.assertEqual(
            large["message"],
            "이미지 OCR 프롬프트는 64 KiB 이하여야 합니다.",
        )
        self.assertIn("64 KiB", large["detail"])

        pdf_body, pdf_type = self._multipart_upload(
            "pdf",
            [
                (
                    "rulebook.pdf",
                    b"%PDF-1.4\ninvalid prompt\n%%EOF\n",
                    "application/pdf",
                )
            ],
            ocr_prompt="PDF does not use this.",
        )
        status, pdf = self._request(
            "/api/projects/invalid_ocr_prompt/source",
            method="POST",
            body=pdf_body,
            content_type=pdf_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(pdf["code"], "SOURCE_REGISTER_FAILED")
        self.assertEqual(
            pdf["message"],
            "OCR 프롬프트는 이미지 원본을 선택했을 때만 사용할 수 있습니다.",
        )
        self.assertIn("image sources", pdf["detail"])

        status, no_images = self._request(
            "/api/projects/invalid_ocr_prompt/ocr-prompt",
            method="PATCH",
            payload={"ocr_prompt": "No registered images."},
        )
        self.assertEqual(status, 400)
        self.assertEqual(no_images["code"], "OCR_PROMPT_UPDATE_FAILED")
        self.assertEqual(
            no_images["message"],
            "이미지 원본이 등록된 프로젝트에서만 OCR 프롬프트를 수정할 수 있습니다.",
        )

    def test_updates_only_ocr_prompt_before_processing(self) -> None:
        location = create_project(
            name="Prompt Only API",
            project_id="prompt_only_api",
            workspace_root=self.workspace_root,
        )
        image_output = BytesIO()
        Image.new("RGB", (8, 8), "white").save(
            image_output,
            format="PNG",
        )
        upload_body, upload_type = self._multipart_upload(
            "images",
            [("card.png", image_output.getvalue(), "image/png")],
            ocr_prompt="Initial prompt.",
        )
        status, _ = self._request(
            "/api/projects/prompt_only_api/source",
            method="POST",
            body=upload_body,
            content_type=upload_type,
        )
        self.assertEqual(status, 201)
        image_path = location.path / "01_input/images/card.png"
        prompt_path = location.path / "01_input/images/ocr_prompt.txt"
        image_before = image_path.read_bytes()

        status, unauthorized = self._request(
            "/api/projects/prompt_only_api/ocr-prompt",
            method="PATCH",
            payload={"ocr_prompt": "Unauthorized edit."},
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        status, updated = self._request(
            "/api/projects/prompt_only_api/ocr-prompt",
            method="PATCH",
            payload={"ocr_prompt": "Edited prompt only."},
        )
        self.assertEqual(status, 200)
        self.assertTrue(updated["ocr_prompt"]["updated"])
        self.assertEqual(
            updated["ocr_prompt"]["path"],
            "01_input/images/ocr_prompt.txt",
        )
        self.assertEqual(
            prompt_path.read_text(encoding="utf-8"),
            "Edited prompt only.\n",
        )
        self.assertEqual(image_path.read_bytes(), image_before)

        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        project = next(
            item
            for item in dashboard["projects"]
            if item["project_id"] == "prompt_only_api"
        )
        self.assertEqual(project["ocr_prompt"], "Edited prompt only.\n")
        self.assertTrue(project["ocr_prompt_edit"]["allowed"])

        (location.path / ".glk/state/image_ocr.json").write_text(
            "{}",
            encoding="utf-8",
        )
        status, blocked = self._request(
            "/api/projects/prompt_only_api/ocr-prompt",
            method="PATCH",
            payload={"ocr_prompt": "Too late."},
        )
        self.assertEqual(status, 400)
        self.assertEqual(blocked["code"], "OCR_PROMPT_UPDATE_FAILED")
        self.assertEqual(
            blocked["message"],
            "OCR이 시작된 뒤에는 프롬프트를 수정할 수 없습니다.",
        )
        self.assertEqual(
            prompt_path.read_text(encoding="utf-8"),
            "Edited prompt only.\n",
        )

    def test_replaces_source_before_processing_and_rejects_after_started(
        self,
    ) -> None:
        location = create_project(
            name="Replace Upload",
            project_id="replace_upload",
            workspace_root=self.workspace_root,
        )
        prompt_path = location.path / "01_input/images/ocr_prompt.txt"
        prompt_before = prompt_path.read_bytes()
        pdf_body, pdf_content_type = self._multipart_upload(
            "pdf",
            [
                (
                    "old.pdf",
                    b"%PDF-1.4\nold dashboard source\n%%EOF\n",
                    "application/pdf",
                )
            ],
        )
        status, _ = self._request(
            "/api/projects/replace_upload/source",
            method="POST",
            body=pdf_body,
            content_type=pdf_content_type,
        )
        self.assertEqual(status, 201)

        image_output = BytesIO()
        Image.new("RGB", (8, 8), "white").save(
            image_output,
            format="PNG",
        )
        image_body, image_content_type = self._multipart_upload(
            "images",
            [("new.png", image_output.getvalue(), "image/png")],
        )
        status, unauthorized = self._request(
            "/api/projects/replace_upload/source",
            method="PUT",
            body=image_body,
            content_type=image_content_type,
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])

        status, replaced = self._request(
            "/api/projects/replace_upload/source",
            method="PUT",
            body=image_body,
            content_type=image_content_type,
        )
        self.assertEqual(status, 200)
        self.assertTrue(replaced["source"]["replaced"])
        self.assertEqual(replaced["source"]["source_type"], "images")
        self.assertFalse(
            (location.path / "01_input/pdf/old.pdf").exists()
        )

        self.assertTrue(
            (location.path / "01_input/images/new.png").is_file()
        )
        self.assertEqual(prompt_path.read_bytes(), prompt_before)

        edited_body, edited_content_type = self._multipart_upload(
            "images",
            [("new.png", image_output.getvalue(), "image/png")],
            ocr_prompt="Use the edited project OCR rules.",
        )
        status, edited = self._request(
            "/api/projects/replace_upload/source",
            method="PUT",
            body=edited_body,
            content_type=edited_content_type,
        )
        self.assertEqual(status, 200)
        self.assertTrue(edited["source"]["ocr_prompt_updated"])
        self.assertEqual(
            prompt_path.read_text(encoding="utf-8"),
            "Use the edited project OCR rules.\n",
        )

        status, dashboard = self._request("/api/dashboard")
        self.assertEqual(status, 200)
        replaced_project = next(
            project
            for project in dashboard["projects"]
            if project["project_id"] == "replace_upload"
        )
        self.assertEqual(replaced_project["source_files"], ["new.png"])

        (location.path / ".glk/state/image_ocr.json").write_text(
            "{}",
            encoding="utf-8",
        )
        status, blocked = self._request(
            "/api/projects/replace_upload/source",
            method="PUT",
            body=pdf_body,
            content_type=pdf_content_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(blocked["code"], "SOURCE_REPLACE_FAILED")
        self.assertTrue(
            (location.path / "01_input/images/new.png").is_file()
        )
        self.assertFalse(
            (location.path / "01_input/pdf/old.pdf").exists()
        )

        acquisition_state = location.path / ".glk/state/image_ocr.json"
        acquisition_state.write_text(
            json.dumps({
                "status": "partial",
                "failures": [
                    {"file": "new.png", "code": "SOURCE_PROCESSING_FAILED"}
                ],
            }),
            encoding="utf-8",
        )
        stale_output = location.path / "02_source/ocr/combined.partial.txt"
        stale_output.write_text("partial output", encoding="utf-8")
        stale_cache = location.path / ".glk/cache/ocr/results/new.json"
        stale_cache.parent.mkdir(parents=True, exist_ok=True)
        stale_cache.write_text("{}", encoding="utf-8")

        status, recovered = self._request(
            "/api/projects/replace_upload/source",
            method="PUT",
            body=pdf_body,
            content_type=pdf_content_type,
        )

        self.assertEqual(status, 200)
        self.assertTrue(recovered["source"]["replaced"])
        self.assertEqual(recovered["source"]["source_type"], "pdf")
        self.assertTrue((location.path / "01_input/pdf/old.pdf").is_file())
        self.assertFalse((location.path / "01_input/images/new.png").exists())
        self.assertFalse(acquisition_state.exists())
        self.assertFalse(stale_output.exists())
        self.assertFalse(stale_cache.exists())

    def test_source_upload_rejects_bad_input_and_unknown_project(self) -> None:
        create_project(
            name="Invalid Upload",
            project_id="invalid_upload",
            workspace_root=self.workspace_root,
        )
        body, content_type = self._multipart_upload(
            "pdf",
            [("not-pdf.txt", b"not a pdf", "text/plain")],
        )
        status, invalid = self._request(
            "/api/projects/invalid_upload/source",
            method="POST",
            body=body,
            content_type=content_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "SOURCE_REGISTER_FAILED")

        valid_body, valid_content_type = self._multipart_upload(
            "pdf",
            [("book.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")],
        )
        status, missing = self._request(
            "/api/projects/not_found/source",
            method="POST",
            body=valid_body,
            content_type=valid_content_type,
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["code"], "RESOURCE_NOT_FOUND")

        status, escaped = self._request(
            "/api/projects/..%2Finvalid_upload/source",
            method="POST",
            body=valid_body,
            content_type=valid_content_type,
        )
        self.assertEqual(status, 400)
        self.assertEqual(escaped["code"], "SOURCE_REGISTER_FAILED")

    @patch("glk.infrastructure.dashboard_server.send2trash")
    def test_moves_project_to_trash_and_rejects_duplicate_delete(
        self,
        mocked_send2trash,
    ) -> None:
        location = create_project(
            name="Delete Me",
            project_id="delete_me",
            workspace_root=self.workspace_root,
        )
        trash_root = Path(self.temporary_directory.name) / "trash"
        trash_root.mkdir()

        def move_to_test_trash(value: str) -> None:
            path = Path(value)
            path.rename(trash_root / path.name)

        mocked_send2trash.side_effect = move_to_test_trash

        status, unauthorized = self._request(
            "/api/projects/delete_me",
            method="DELETE",
            authorized=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(unauthorized["ok"])
        self.assertTrue(location.path.is_dir())

        status, deleted = self._request(
            "/api/projects/delete_me",
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["project"]["project_id"], "delete_me")
        mocked_send2trash.assert_called_once_with(str(location.path))
        self.assertFalse(location.path.exists())
        self.assertTrue((trash_root / "delete_me").is_dir())

        status, missing = self._request(
            "/api/projects/delete_me",
            method="DELETE",
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["code"], "RESOURCE_NOT_FOUND")

    @patch("glk.infrastructure.dashboard_server.send2trash")
    def test_delete_rejects_path_escape_and_unknown_project(
        self,
        mocked_send2trash,
    ) -> None:
        status, invalid = self._request(
            "/api/projects/..%2Foutside",
            method="DELETE",
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "PROJECT_DELETE_FAILED")

        status, missing = self._request(
            "/api/projects/not_found",
            method="DELETE",
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["code"], "RESOURCE_NOT_FOUND")
        mocked_send2trash.assert_not_called()

    @patch(
        "glk.infrastructure.dashboard_server.send2trash",
        side_effect=OSError("trash unavailable"),
    )
    def test_delete_reports_trash_failure(
        self,
        mocked_send2trash,
    ) -> None:
        location = create_project(
            name="Keep Me",
            project_id="keep_me",
            workspace_root=self.workspace_root,
        )

        status, failed = self._request(
            "/api/projects/keep_me",
            method="DELETE",
        )

        self.assertEqual(status, 500)
        self.assertEqual(failed["code"], "PROJECT_DELETE_FAILED")
        self.assertIn("휴지통", failed["message"])
        self.assertTrue(location.path.is_dir())
        mocked_send2trash.assert_called_once_with(str(location.path))


if __name__ == "__main__":
    unittest.main()
