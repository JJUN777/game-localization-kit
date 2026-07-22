from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pymupdf

from glk.cli import EXIT_NOT_IMPLEMENTED, main


class CliTests(unittest.TestCase):
    def test_no_command_prints_help(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("Game localization pipeline", output.getvalue())

    def test_version_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["version"])
        self.assertEqual(exit_code, 0)
        self.assertRegex(output.getvalue(), r"^glk \d+\.\d+\.\d+\n$")

    def test_planned_command_fails_explicitly_as_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            exit_code = main(["translate", "--file", "sample.txt", "--json"])
        self.assertEqual(exit_code, EXIT_NOT_IMPLEMENTED)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "translate")
        self.assertEqual(payload["code"], "NOT_IMPLEMENTED")

    def test_ocr_dry_run_reports_selected_images_as_json(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            image_folder = root / "images"
            image_folder.mkdir()
            Image.new("RGB", (8, 8), "white").save(image_folder / "card.png")
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "OCR Cards",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "ocr",
                        "--project",
                        "ocr_cards",
                        "--folder",
                        str(image_folder),
                        "--workspace-root",
                        str(workspace_root),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["selected_images"], ["card.png"])

    def test_init_and_status_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            init_output = io.StringIO()
            with redirect_stdout(init_output):
                init_exit_code = main(
                    [
                        "init",
                        "Primal Rulebook",
                        "--profile",
                        "primal",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(init_exit_code, 0)
            init_payload = json.loads(init_output.getvalue())
            self.assertTrue(init_payload["ok"])
            self.assertEqual(init_payload["manifest"]["project_id"], "primal_rulebook")

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                status_exit_code = main(
                    [
                        "status",
                        "--project",
                        "primal_rulebook",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(status_exit_code, 0)
            status_payload = json.loads(status_output.getvalue())
            self.assertTrue(status_payload["ok"])
            self.assertEqual(status_payload["missing_paths"], [])

    def test_extract_dry_run_does_not_require_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            pdf_path = root / "sample.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Sample page")
            document.save(pdf_path)
            document.close()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "init",
                            "Sample",
                            "--workspace-root",
                            str(workspace_root),
                            "--json",
                        ]
                    ),
                    0,
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "extract",
                        "--project",
                        "sample",
                        "--file",
                        str(pdf_path),
                        "--workspace-root",
                        str(workspace_root),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertFalse((workspace_root / "sample/source/original.pdf").exists())


if __name__ == "__main__":
    unittest.main()
