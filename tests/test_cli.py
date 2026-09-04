from __future__ import annotations

import errno
import io
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image

from glk.cli import main
from glk.application.extraction_service import ExtractionResult, PageFailure
from glk.application.glossary_service import GlossaryBuildResult, GlossaryImportResult
from glk.application.segmentation_service import SegmentationResult
from glk.application.source_qa_service import SourceQaResult
from glk.application.translation_service import TranslationRunResult
from glk.application.translation_retry_service import TranslationRetryResult
from glk.application.translation_review_service import (
    TranslationFinalizeResult,
    TranslationQaResult,
)
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock


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
        self.assertEqual(output.getvalue(), "glk 2.5.0\n")

    def test_ui_command_starts_local_dashboard(self) -> None:
        with patch("glk.cli.serve_dashboard") as serve:
            exit_code = main(
                [
                    "ui",
                    "--workspace-root",
                    "custom-workspaces",
                    "--settings-root",
                    "custom-settings",
                    "--port",
                    "8765",
                    "--no-open",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            serve.call_args.kwargs,
            {
                "workspace_root": "custom-workspaces",
                "settings_root": "custom-settings",
                "port": 8765,
                "open_browser": False,
            },
        )

    def test_ui_command_uses_stable_default_port(self) -> None:
        with patch("glk.cli.serve_dashboard") as serve:
            exit_code = main(["ui", "--no-open"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(serve.call_args.kwargs["port"], 8765)

    def test_ui_command_explains_when_port_is_already_in_use(self) -> None:
        error_output = io.StringIO()
        with (
            patch(
                "glk.cli.serve_dashboard",
                side_effect=OSError(errno.EADDRINUSE, "Address already in use"),
            ),
            redirect_stderr(error_output),
        ):
            exit_code = main(["ui", "--no-open"])

        self.assertEqual(exit_code, 1)
        self.assertIn("대시보드 포트를 이미 사용 중입니다.", error_output.getvalue())
        self.assertIn("--port", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())

    def test_projects_command_lists_workspace_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "First Game",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
                main(
                    [
                        "init",
                        "Second Game",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "projects",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                [project["project_id"] for project in payload["projects"]],
                ["first_game", "second_game"],
            )

    def test_retry_failed_command_reports_result_as_json(self) -> None:
        result = TranslationRetryResult(
            project_path="/tmp/workspaces/game",
            model="test-model",
            requested_blocks=2,
            retried_blocks=2,
            block_ids=("block-1", "block-2"),
            previous_error_count=3,
            remaining_error_count=0,
            warning_count=1,
            review_file="/tmp/workspaces/game/04_translation/review.txt",
            revision_file="/tmp/workspaces/game/04_translation/revisions/retry.json",
        )
        output = io.StringIO()
        with (
            patch("glk.cli.retry_failed_translations", return_value=result) as retry,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                ["retry", "--failed", "--project", "game", "--json"]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["retried_blocks"], 2)
        self.assertEqual(retry.call_args.kwargs["project"], "game")

    def test_ocr_dry_run_reports_selected_images_as_json(self) -> None:
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

    def test_run_dispatches_pdf_non_interactively(self) -> None:
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
                main(
                    [
                        "init",
                        "Run PDF",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "run_pdf",
                        "--input-type",
                        "pdf",
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
            self.assertEqual(payload["selected_pages"], [1])

    def test_run_dispatches_image_folder_from_folder_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            image_folder = root / "images"
            nested_folder = image_folder / "characters"
            nested_folder.mkdir(parents=True)
            Image.new("RGB", (8, 8), "white").save(nested_folder / "card.png")
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Run Images",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "run_images",
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
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["selected_images"], ["characters/card.png"])

    def test_run_auto_detects_pdf_from_project_input_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Default PDF",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            pdf_path = workspace_root / "default_pdf/01_input/pdf/rulebook.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Sample page")
            document.save(pdf_path)
            document.close()

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "default_pdf",
                        "--workspace-root",
                        str(workspace_root),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["input_type"], "pdf")
            self.assertEqual(Path(payload["source_pdf"]).resolve(), pdf_path.resolve())

    def test_run_auto_detects_images_from_project_input_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Default Images",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            image_folder = workspace_root / "default_images/01_input/images"
            nested = image_folder / "characters"
            nested.mkdir()
            Image.new("RGB", (8, 8), "white").save(nested / "card.png")

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "default_images",
                        "--workspace-root",
                        str(workspace_root),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["input_type"], "images")
            self.assertEqual(payload["selected_images"], ["characters/card.png"])

    def test_run_prepares_review_and_qa_after_successful_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Integrated Run",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            project_path = workspace_root / "integrated_run"
            acquisition = ExtractionResult(
                project_path=str(project_path),
                source_pdf=str(project_path / "01_input/pdf/rulebook.pdf"),
                source_sha256="a" * 64,
                model="test-model",
                prompt_version="test-v1",
                selected_pages=(1, 2),
                successful_pages=(1, 2),
                cached_pages=(),
                failures=(),
                output_file=str(project_path / "02_source/extracted.txt"),
            )
            segmentation = SegmentationResult(
                project_path=str(project_path),
                source_type="pdf",
                input_sha256="b" * 64,
                total_blocks=12,
                flagged_blocks=0,
                output_file=str(project_path / ".glk/segments/source.jsonl"),
                draft_file=str(project_path / "02_source/draft.txt"),
                review_file=str(project_path / "02_source/review.txt"),
                review_status="current",
                review_created=True,
            )
            qa = SourceQaResult(
                project_path=str(project_path),
                input_sha256="c" * 64,
                total_blocks=12,
                flagged_blocks=2,
                total_issues=3,
                error_count=1,
                warning_count=2,
                info_count=0,
                allowed_tokens=(),
                output_file=str(project_path / ".glk/reports/source_qa.json"),
                human_report_file=str(project_path / "02_source/qa.md"),
            )
            output = io.StringIO()
            with (
                patch("glk.cli.extract_project_pdf", return_value=acquisition) as acquire,
                patch("glk.cli.segment_project_source", return_value=segmentation) as segment,
                patch("glk.cli.run_project_source_qa", return_value=qa) as source_qa,
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "integrated_run",
                        "--input-type",
                        "pdf",
                        "--file",
                        "rulebook.pdf",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["pipeline_status"], "awaiting_human_review")
            self.assertEqual(payload["segmentation"]["total_blocks"], 12)
            self.assertEqual(payload["qa"]["total_issues"], 3)
            self.assertEqual(payload["review_file"], segmentation.review_file)
            acquire.assert_called_once()
            segment.assert_called_once()
            source_qa.assert_called_once()

    def test_run_stops_before_review_preparation_on_partial_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Partial Run",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            acquisition = ExtractionResult(
                project_path=str(workspace_root / "partial_run"),
                source_pdf="01_input/pdf/rulebook.pdf",
                source_sha256="a" * 64,
                model="test-model",
                prompt_version="test-v1",
                selected_pages=(1, 2),
                successful_pages=(1,),
                cached_pages=(),
                failures=(PageFailure(page=2, error="layout failed"),),
                output_file="02_source/extracted.txt",
            )
            output = io.StringIO()
            with (
                patch("glk.cli.extract_project_pdf", return_value=acquisition),
                patch("glk.cli.segment_project_source") as segment,
                patch("glk.cli.run_project_source_qa") as source_qa,
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "partial_run",
                        "--file",
                        "rulebook.pdf",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 4)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["pipeline_status"], "acquisition_partial")
            segment.assert_not_called()
            source_qa.assert_not_called()

    def test_run_interactive_wizard_asks_only_for_type_and_source_path(self) -> None:
        class InteractiveInput(io.StringIO):
            def isatty(self) -> bool:
                return True

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
                        "Wizard Images",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            interactive_input = InteractiveInput(f"2\n{image_folder}\n")
            original_stdin = sys.stdin
            try:
                sys.stdin = interactive_input
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    exit_code = main(
                        [
                            "run",
                            "--project",
                            "wizard_images",
                            "--workspace-root",
                            str(workspace_root),
                            "--dry-run",
                        ]
                    )
            finally:
                sys.stdin = original_stdin
            self.assertEqual(exit_code, 0)
            self.assertIn("1. PDF", output.getvalue())
            self.assertIn("2. 이미지 폴더", output.getvalue())
            self.assertIn("Would OCR 1 images", output.getvalue())

    def test_run_rejects_file_and_folder_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "Invalid Run",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        "invalid_run",
                        "--file",
                        "a.pdf",
                        "--folder",
                        "images",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "RUN_INPUT_FAILED")
            self.assertIn("원문 입력", payload["message"])
            self.assertIsInstance(payload["detail"], str)

    def test_segment_reports_missing_source_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "No Source",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "segment",
                        "--project",
                        "no_source",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "SEGMENTATION_FAILED")
            self.assertIn("검수 블록", payload["message"])
            self.assertIsInstance(payload["detail"], str)

    def test_qa_reports_missing_common_blocks_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "No Segments",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "qa",
                        "--project",
                        "no_segments",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "SOURCE_QA_FAILED")
            self.assertIn("원문 자동 QA", payload["message"])
            self.assertIsInstance(payload["detail"], str)

    def test_review_prepare_and_finalize_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "init",
                        "CLI Review",
                        "--workspace-root",
                        str(workspace_root),
                    ]
                )
            text = "Gain l0 health."
            block = SourceBlock(
                schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
                id="pdf-p0001-b0001-0000000001",
                source_type="pdf",
                source_file="01_input/pdf/rulebook.pdf",
                page=1,
                source_order=1,
                block_order=1,
                block_type="body",
                raw_text=text,
                corrected_text=None,
                bbox=(100.0, 100.0, 900.0, 900.0),
                legibility=None,
                status="raw",
                warnings=(),
                source_refs=("P001-F001",),
                source_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
            )
            source_path = workspace_root / "cli_review/.glk/segments/source.jsonl"
            source_path.write_text(
                json.dumps(block.to_dict(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            prepare_output = io.StringIO()
            with redirect_stdout(prepare_output), redirect_stderr(io.StringIO()):
                prepare_exit = main(
                    [
                        "review",
                        "prepare",
                        "--project",
                        "cli_review",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(prepare_exit, 0)
            self.assertTrue(json.loads(prepare_output.getvalue())["review_created"])
            review_path = workspace_root / "cli_review/02_source/review.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace("l0", "10"),
                encoding="utf-8",
            )

            finalize_output = io.StringIO()
            with redirect_stdout(finalize_output), redirect_stderr(io.StringIO()):
                finalize_exit = main(
                    [
                        "review",
                        "finalize",
                        "--project",
                        "cli_review",
                        "--workspace-root",
                        str(workspace_root),
                        "--json",
                    ]
                )
            self.assertEqual(finalize_exit, 0)
            payload = json.loads(finalize_output.getvalue())
            self.assertEqual(payload["changed_blocks"], 1)
            self.assertTrue((workspace_root / "cli_review/02_source/final.txt").is_file())

    def test_glossary_build_command_outputs_single_json_result(self) -> None:
        result = GlossaryBuildResult(
            project_path="/tmp/workspaces/game",
            approved_source_sha256="a" * 64,
            candidate_count=17,
            output_file="/tmp/workspaces/game/03_terminology/glossary_review.tsv",
            status="current",
            created=True,
        )
        output = io.StringIO()
        with (
            patch("glk.cli.build_project_glossary_candidates", return_value=result) as build,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                [
                    "glossary",
                    "build",
                    "--project",
                    "game",
                    "--min-frequency",
                    "3",
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["candidate_count"], 17)
        self.assertTrue(payload["created"])
        self.assertEqual(build.call_args.kwargs["min_frequency"], 3)

    def test_glossary_import_command_outputs_single_json_result(self) -> None:
        result = GlossaryImportResult(
            project_path="/tmp/workspaces/game",
            approved_source_sha256="a" * 64,
            review_tsv_sha256="b" * 64,
            entry_count=18,
            active_count=12,
            rejected_count=6,
            manual_count=2,
            unverified_count=1,
            review_file="/tmp/workspaces/game/03_terminology/glossary_review.tsv",
            output_file="/tmp/workspaces/game/03_terminology/termbase.json",
            warnings=("One manual term is unverified.",),
        )
        output = io.StringIO()
        with (
            patch("glk.cli.import_project_glossary", return_value=result) as glossary_import,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                [
                    "glossary",
                    "import",
                    "--project",
                    "game",
                    "--file",
                    "03_terminology/glossary_review.tsv",
                    "--allow-missing-terms",
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["entry_count"], 18)
        self.assertEqual(payload["manual_count"], 2)
        self.assertEqual(payload["unverified_count"], 1)
        self.assertTrue(glossary_import.call_args.kwargs["allow_missing_terms"])

    def test_translate_command_outputs_single_json_result(self) -> None:
        result = TranslationRunResult(
            project_path="/tmp/workspaces/game",
            model="test-model",
            approved_source_sha256="a" * 64,
            termbase_sha256="b" * 64,
            project_prompt_sha256="c" * 64,
            input_sha256="d" * 64,
            total_blocks=24,
            total_chunks=3,
            completed_blocks=24,
            completed_chunks=3,
            output_file="/tmp/workspaces/game/.glk/segments/translation.jsonl",
            draft_file="/tmp/workspaces/game/04_translation/draft.txt",
            review_file="/tmp/workspaces/game/04_translation/review.txt",
            review_status="current",
            prompt_file="/tmp/workspaces/game/04_translation/prompt.txt",
            review_created=True,
        )
        output = io.StringIO()
        with (
            patch("glk.cli.translate_project", return_value=result) as translate,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                [
                    "translate",
                    "--project",
                    "game",
                    "--prompt",
                    "project_prompt.txt",
                    "--model",
                    "test-model",
                    "--max-characters",
                    "8000",
                    "--resume",
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["completed_blocks"], 24)
        self.assertEqual(payload["review_status"], "current")
        self.assertEqual(translate.call_args.kwargs["max_characters"], 8000)
        self.assertTrue(translate.call_args.kwargs["resume"])

    def test_translation_qa_and_finalize_commands(self) -> None:
        qa_result = TranslationQaResult(
            project_path="/tmp/workspaces/game",
            total_blocks=24,
            error_count=0,
            warning_count=1,
            info_count=0,
            issues=(),
            json_report="/tmp/workspaces/game/.glk/reports/translation_qa.json",
            markdown_report="/tmp/workspaces/game/04_translation/qa.md",
        )
        qa_output = io.StringIO()
        with (
            patch("glk.cli.run_project_translation_qa", return_value=qa_result) as qa,
            redirect_stdout(qa_output),
            redirect_stderr(io.StringIO()),
        ):
            qa_exit = main(
                [
                    "translation",
                    "qa",
                    "--project",
                    "game",
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(qa_exit, 0)
        self.assertTrue(json.loads(qa_output.getvalue())["passed"])
        self.assertTrue(qa.call_args.kwargs["dry_run"])

        finalize_result = TranslationFinalizeResult(
            project_path="/tmp/workspaces/game",
            total_blocks=24,
            changed_blocks=5,
            error_count=0,
            warning_count=1,
            issues=(),
            output_file="/tmp/workspaces/game/05_output/rulebook_kor.txt",
            approved_segments_file=(
                "/tmp/workspaces/game/.glk/segments/approved_translation.jsonl"
            ),
            json_report="/tmp/workspaces/game/.glk/reports/translation_qa.json",
            markdown_report="/tmp/workspaces/game/04_translation/qa.md",
            finalized=True,
        )
        finalize_output = io.StringIO()
        with (
            patch(
                "glk.cli.finalize_project_translation_review",
                return_value=finalize_result,
            ) as finalize,
            redirect_stdout(finalize_output),
            redirect_stderr(io.StringIO()),
        ):
            finalize_exit = main(
                [
                    "translation",
                    "finalize",
                    "--project",
                    "game",
                    "--json",
                ]
            )
        self.assertEqual(finalize_exit, 0)
        payload = json.loads(finalize_output.getvalue())
        self.assertTrue(payload["finalized"])
        self.assertEqual(payload["changed_blocks"], 5)
        self.assertEqual(finalize.call_args.kwargs["project"], "game")

    def test_review_translation_command_starts_local_ui(self) -> None:
        with patch("glk.cli.serve_translation_review") as serve:
            exit_code = main(
                [
                    "review",
                    "translation",
                    "--project",
                    "game",
                    "--port",
                    "8765",
                    "--no-open",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(serve.call_args.kwargs["project"], "game")
        self.assertEqual(serve.call_args.kwargs["port"], 8765)
        self.assertFalse(serve.call_args.kwargs["open_browser"])

    def test_review_source_command_starts_local_ui(self) -> None:
        with patch("glk.cli.serve_source_review") as serve:
            exit_code = main(
                [
                    "review",
                    "source",
                    "--project",
                    "game",
                    "--port",
                    "8766",
                    "--no-open",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(serve.call_args.kwargs["project"], "game")
        self.assertEqual(serve.call_args.kwargs["port"], 8766)
        self.assertFalse(serve.call_args.kwargs["open_browser"])

    def test_review_glossary_command_starts_local_ui(self) -> None:
        with patch("glk.cli.serve_glossary_review") as serve:
            exit_code = main(
                [
                    "review",
                    "glossary",
                    "--project",
                    "game",
                    "--port",
                    "8767",
                    "--no-open",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(serve.call_args.kwargs["project"], "game")
        self.assertEqual(serve.call_args.kwargs["port"], 8767)
        self.assertFalse(serve.call_args.kwargs["open_browser"])

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
            self.assertFalse((workspace_root / "sample/01_input/pdf/sample.pdf").exists())


if __name__ == "__main__":
    unittest.main()
