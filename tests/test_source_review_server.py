from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image

from glk.application.project_service import create_project, update_project_source
from glk.application.source_review_service import prepare_project_source_review
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock
from glk.infrastructure.source_review_server import (
    SourceReviewHttpServer,
    create_source_review_server,
)
from glk.infrastructure.ai_usage import AiUsageAccumulator
from tests.test_source_review_service import make_block, write_blocks


def make_image_block(source_file: str) -> SourceBlock:
    text = "Gain 10 [HP]."

    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id="image-card-b0001-0000000001",
        source_type="image",
        source_file=source_file,
        page=None,
        source_order=1,
        block_order=1,
        block_type="body",
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility="clear",
        status="raw",
        warnings=(),
        source_refs=("card.png",),
        source_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


class FakePdfIconAuditProvider:
    model_name = "test-icon-model"

    def __init__(self) -> None:
        self.usage = AiUsageAccumulator("gemini", self.model_name)
        self.prompts: list[str] = []

    def inspect(self, prompt: str, image: Image.Image) -> dict[str, object]:
        self.prompts.append(prompt)
        self.usage.begin_request()
        self.usage.input_tokens += 120
        self.usage.output_tokens += 20
        self.last_image_size = image.size
        return {
            "icons": [
                {
                    "marker": "[DAMAGE]",
                    "description": "orange diamond",
                    "after_unit_id": "U001",
                    "confidence": "high",
                }
            ],
            "summary": "One meaningful icon found.",
        }


class SourceReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary_directory.name) / "workspaces"
        location = create_project(name="Visual Source", workspace_root=self.workspace_root)
        self.project_path = location.path
        pdf_path = self.project_path / "01_input/pdf/rulebook.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nvisual-source\n")
        update_project_source(location, "01_input/pdf/rulebook.pdf")
        write_blocks(
            self.project_path / ".glk/segments/source.jsonl",
            [make_block(1, "First text."), make_block(2, "Second text.")],
        )
        image_path = self.project_path / ".glk/cache/pdf/pages/page_001.png"
        Image.new("RGB", (40, 60), "white").save(image_path)
        prepare_project_source_review(
            project="visual_source", workspace_root=self.workspace_root
        )
        self.server: SourceReviewHttpServer = create_source_review_server(
            project="visual_source", workspace_root=self.workspace_root
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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
                content_type = response.headers.get("Content-Type", "")
                raw = response.read()
                if content_type.startswith("application/json"):
                    return response.status, json.loads(raw.decode("utf-8"))
                return response.status, raw.decode("utf-8")
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_serves_ui_and_protected_source_assets(self) -> None:
        status, html = self._request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("원문 이미지 · 추출문 검수", html)
        self.assertIn("검증 전 미해결", html)
        self.assertIn('id="unresolvedFilter"', html)
        self.assertIn('id="approveUnresolvedIcons"', html)
        self.assertNotIn('id="allowTokens"', html)
        self.assertIn("아이콘 token이 변경된 블록", html)
        self.assertIn('id="iconAuditMode"', html)
        self.assertIn('id="iconAuditRun"', html)
        self.assertIn("선택 블록 AI 검사", html)
        self.assertIn("AI 제안", html)
        self.assertIn("저장된 결과", html)
        self.assertIn("예상 비용", html)
        self.assertIn("copy.textContent = value", html)
        self.assertNotIn("__GLK_TOKEN_JSON__", html)
        self.assertNotIn("__GLK_RETURN_URL_JSON__", html)
        self.assertIn("const RETURN_URL = null;", html)
        self.assertIn("원문 승인이 완료되었습니다", html)
        self.assertIn("추출 경고", html)
        self.assertIn('$("addMissing").textContent = active ? "추가 취소"', html)
        self.assertIn("layoutWarningsOnly = false;\n      unresolvedOnly = false;", html)
        self.assertIn("const groupItems = allGroupBlocks();", html)

        status, payload = self._request("/api/review", authorized=False)
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        status, payload = self._request(
            "/api/review", origin="https://attacker.example"
        )
        self.assertEqual(status, 403)

        with urlopen(
            self.server.origin
            + "/api/source-image?group=group-1&token="
            + quote(self.server.auth_token),
            timeout=3,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))

        request = Request(
            self.server.origin
            + "/api/original-pdf?token="
            + quote(self.server.auth_token),
            headers={"Range": "bytes=0-7"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"%PDF-1.4")

    def test_audits_selected_pdf_block_and_serves_its_crop(self) -> None:
        prompt_path = self.project_path / "01_input/images/ocr_prompt.txt"
        prompt_path.write_text(
            "- [DAMAGE]: orange diamond with no inner mark.\n",
            encoding="utf-8",
        )
        provider = FakePdfIconAuditProvider()
        self.server.icon_audit_provider = provider
        status, document = self._request("/api/review")
        self.assertEqual(status, 200)
        block = document["blocks"][0]

        status, result = self._request(
            "/api/icon-audit",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "blocks": [{"id": block["id"], "text": block["text"]}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["inspected_blocks"], 1)
        self.assertEqual(result["detected_icons"], 1)
        self.assertEqual(result["usage"]["requests"], 1)
        audited = result["results"][0]
        self.assertEqual(audited["current_text"], "First text.")
        self.assertEqual(audited["suggested_text"], "First [DAMAGE] text.")
        self.assertFalse(audited["cached"])
        self.assertIn("Never rewrite", provider.prompts[0])
        self.assertTrue(
            (self.project_path / ".glk/state/pdf_icon_audit.json").is_file()
        )

        status, cached_result = self._request(
            "/api/icon-audit",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "blocks": [{"id": block["id"], "text": block["text"]}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(cached_result["cached_blocks"], 1)
        self.assertTrue(cached_result["results"][0]["cached"])
        self.assertEqual(cached_result["usage"]["requests"], 0)
        self.assertEqual(len(provider.prompts), 1)

        status, changed_result = self._request(
            "/api/icon-audit",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "blocks": [{"id": block["id"], "text": "Changed text."}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed_result["cached_blocks"], 0)
        self.assertEqual(changed_result["usage"]["requests"], 1)
        self.assertEqual(len(provider.prompts), 2)
        ledger_lines = (
            self.project_path / ".glk/state/ai_usage.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger_lines), 2)
        ledger_events = [json.loads(line) for line in ledger_lines]
        self.assertTrue(
            all(event["stage"] == "source_review" for event in ledger_events)
        )
        self.assertTrue(
            all(event["usage"]["requests"] == 1 for event in ledger_events)
        )

        crop_url = (
            self.server.origin
            + audited["crop_url"]
            + "&token="
            + quote(self.server.auth_token)
        )
        with urlopen(crop_url, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))

    def test_icon_audit_requires_current_review_hash(self) -> None:
        self.server.icon_audit_provider = FakePdfIconAuditProvider()
        status, document = self._request("/api/review")
        self.assertEqual(status, 200)

        status, response = self._request(
            "/api/icon-audit",
            method="POST",
            payload={
                "review_sha256": "sha256:stale",
                "blocks": [
                    {
                        "id": document["blocks"][0]["id"],
                        "text": document["blocks"][0]["text"],
                    }
                ],
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "PDF_ICON_AUDIT_FAILED")

    def test_save_validate_finalize_and_conflict(self) -> None:
        status, document = self._request("/api/review")
        self.assertEqual(status, 200)
        original_hash = document["review_sha256"]
        blocks = document["blocks"]
        blocks[0]["text"] = "First corrected."

        status, saved = self._request(
            "/api/save",
            method="POST",
            payload={"review_sha256": original_hash, "blocks": blocks},
        )
        self.assertEqual(status, 200)
        new_hash = saved["document"]["review_sha256"]
        self.assertNotEqual(new_hash, original_hash)

        status, conflict = self._request(
            "/api/save",
            method="POST",
            payload={"review_sha256": original_hash, "blocks": blocks},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["code"], "REVIEW_CONFLICT")
        self.assertIn("새로고침", conflict["message"])
        self.assertIn("changed after", conflict["detail"])

        status, validated = self._request(
            "/api/validate",
            method="POST",
            payload={
                "review_sha256": new_hash,
                "blocks": saved["document"]["blocks"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(validated["result"]["dry_run"])

        status, finalized = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": validated["document"]["review_sha256"],
                "blocks": validated["document"]["blocks"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(finalized["document"]["review_status"], "approved")
        self.assertTrue((self.project_path / "02_source/final.txt").is_file())

    def test_validate_reports_unresolved_ocr_text_as_review_guidance(self) -> None:
        status, document = self._request("/api/review")
        self.assertEqual(status, 200)
        document["blocks"][0]["text"] = "Skill [ICON: gray circular symbol]"

        status, response = self._request(
            "/api/validate",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "blocks": document["blocks"],
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "SOURCE_REVIEW_UNRESOLVED_TEXT")
        self.assertIn("미확정 아이콘", response["message"])
        self.assertIn("unresolved icon", response["detail"])

    def test_can_explicitly_approve_unresolved_icon_descriptions(self) -> None:
        status, document = self._request("/api/review")
        self.assertEqual(status, 200)
        document["blocks"][0]["text"] = "Skill [ICON: gray circular symbol]"

        status, response = self._request(
            "/api/finalize",
            method="POST",
            payload={
                "review_sha256": document["review_sha256"],
                "blocks": document["blocks"],
                "allow_unresolved_icons": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["result"]["unresolved_icons_allowed"])
        self.assertEqual(response["result"]["unresolved_icon_blocks"], 1)
        self.assertEqual(response["document"]["review_status"], "approved")

    def test_serves_registered_image_source_without_a_pdf(self) -> None:
        location = create_project(name="Image Visual", workspace_root=self.workspace_root)
        source_file = "01_input/images/cards/card.png"
        image_path = location.path / source_file
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 48), "blue").save(image_path)
        update_project_source(location, "01_input/images")
        write_blocks(
            location.path / ".glk/segments/source.jsonl",
            [make_image_block(source_file)],
        )
        prepare_project_source_review(
            project="image_visual", workspace_root=self.workspace_root
        )
        server = create_source_review_server(
            project="image_visual", workspace_root=self.workspace_root
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                server.origin + "/api/review",
                headers={"X-GLK-Token": server.auth_token},
            )
            with urlopen(request, timeout=3) as response:
                document = json.loads(response.read().decode("utf-8"))
            self.assertIsNone(document["original_pdf_url"])
            self.assertEqual(document["groups"][0]["source_type"], "image")
            group_id = quote(document["groups"][0]["id"])
            with urlopen(
                server.origin
                + f"/api/source-image?group={group_id}&token="
                + quote(server.auth_token),
                timeout=3,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertTrue(response.read().startswith(b"\x89PNG"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_injects_only_a_local_return_url(self) -> None:
        return_url = "http://127.0.0.1:8765/"
        server = create_source_review_server(
            project="visual_source",
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
            create_source_review_server(
                project="visual_source",
                workspace_root=self.workspace_root,
                return_url="https://attacker.example/",
            )


if __name__ == "__main__":
    unittest.main()
