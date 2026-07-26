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
from tests.test_source_review_service import make_block, write_blocks


def make_image_block(source_file: str) -> SourceBlock:
    text = "Gain 10 {HP}."

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
        self.assertNotIn("__GLK_TOKEN_JSON__", html)
        self.assertNotIn("__GLK_RETURN_URL_JSON__", html)
        self.assertIn("const RETURN_URL = null;", html)
        self.assertIn("원문 승인이 완료되었습니다", html)
        self.assertIn("추출 경고", html)

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
