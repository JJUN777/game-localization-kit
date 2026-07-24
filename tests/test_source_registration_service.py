from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from glk.application.project_service import create_project, load_project
from glk.application.source_registration_service import (
    SourceRegistrationError,
    register_project_images,
    register_project_pdf,
    replace_project_images,
    replace_project_pdf,
    update_project_ocr_prompt,
)


class SourceRegistrationServiceTests(unittest.TestCase):
    def test_registers_pdf_without_running_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_pdf = root / "rulebook.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\nregistration only\n%%EOF\n")
            create_project(name="PDF Upload", workspace_root=workspace_root)

            result = register_project_pdf(
                project="pdf_upload",
                file=source_pdf,
                workspace_root=workspace_root,
            )

            project_path = workspace_root / "pdf_upload"
            self.assertEqual(result.source_type, "pdf")
            self.assertEqual(result.files, ("01_input/pdf/rulebook.pdf",))
            self.assertEqual(
                (project_path / result.files[0]).read_bytes(),
                source_pdf.read_bytes(),
            )
            self.assertEqual(
                load_project("pdf_upload", workspace_root).manifest.source_file,
                "01_input/pdf/rulebook.pdf",
            )
            self.assertFalse(
                (project_path / ".glk/state/pdf_acquisition.json").exists()
            )

    def test_registers_images_in_natural_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_images = root / "images"
            source_images.mkdir()
            Image.new("RGB", (8, 8), "white").save(
                source_images / "card-10.png"
            )
            Image.new("RGB", (8, 8), "black").save(
                source_images / "card-2.png"
            )
            create_project(name="Image Upload", workspace_root=workspace_root)

            result = register_project_images(
                project="image_upload",
                folder=source_images,
                workspace_root=workspace_root,
                ocr_prompt="Read title, cost, body, then footer.",
            )

            self.assertEqual(result.source_type, "images")
            self.assertEqual(
                result.files,
                (
                    "01_input/images/card-2.png",
                    "01_input/images/card-10.png",
                ),
            )
            self.assertEqual(
                load_project(
                    "image_upload",
                    workspace_root,
                ).manifest.source_file,
                "01_input/images",
            )
            self.assertFalse(
                (
                    workspace_root
                    / "image_upload/.glk/state/image_ocr.json"
                ).exists()
            )
            self.assertEqual(
                (
                    workspace_root
                    / "image_upload/01_input/images/ocr_prompt.txt"
                ).read_text(encoding="utf-8"),
                "Read title, cost, body, then footer.\n",
            )

    def test_updates_only_prompt_before_ocr_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_images = root / "images"
            source_images.mkdir()
            Image.new("RGB", (8, 8), "white").save(
                source_images / "card.png"
            )
            location = create_project(
                name="Prompt Only",
                workspace_root=workspace_root,
            )
            register_project_images(
                project="prompt_only",
                folder=source_images,
                workspace_root=workspace_root,
            )
            registered_image = (
                location.path / "01_input/images/card.png"
            )
            image_before = registered_image.read_bytes()

            prompt_path = update_project_ocr_prompt(
                project="prompt_only",
                ocr_prompt="Edited without replacing source images.",
                workspace_root=workspace_root,
            )

            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                "Edited without replacing source images.\n",
            )
            self.assertEqual(registered_image.read_bytes(), image_before)
            (location.path / ".glk/state/image_ocr.json").write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SourceRegistrationError,
                "after OCR has started",
            ):
                update_project_ocr_prompt(
                    project="prompt_only",
                    ocr_prompt="Too late.",
                    workspace_root=workspace_root,
                )
            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                "Edited without replacing source images.\n",
            )

    def test_rejects_a_different_registered_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\nfirst\n")
            second_pdf.write_bytes(b"%PDF-1.4\nsecond\n")
            create_project(name="No Replace", workspace_root=workspace_root)
            register_project_pdf(
                project="no_replace",
                file=first_pdf,
                workspace_root=workspace_root,
            )

            with self.assertRaises(SourceRegistrationError):
                register_project_pdf(
                    project="no_replace",
                    file=second_pdf,
                    workspace_root=workspace_root,
                )

    def test_rejects_image_output_name_collisions_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_images = root / "images"
            source_images.mkdir()
            Image.new("RGB", (8, 8), "white").save(
                source_images / "card.png"
            )
            Image.new("RGB", (8, 8), "black").save(
                source_images / "card.jpg"
            )
            location = create_project(
                name="Image Collision",
                workspace_root=workspace_root,
            )

            with self.assertRaises(SourceRegistrationError):
                register_project_images(
                    project="image_collision",
                    folder=source_images,
                    workspace_root=workspace_root,
                )

            self.assertIsNone(
                load_project(
                    "image_collision",
                    workspace_root,
                ).manifest.source_file
            )
            self.assertFalse(
                (location.path / "01_input/images/card.png").exists()
            )

    def test_replaces_unprocessed_pdf_with_images_and_preserves_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            first_pdf = root / "first.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\nfirst\n")
            images = root / "images"
            images.mkdir()
            Image.new("RGB", (8, 8), "white").save(images / "card.png")
            location = create_project(
                name="Replace Source",
                workspace_root=workspace_root,
            )
            prompt = location.path / "01_input/images/ocr_prompt.txt"
            prompt.write_text("Project-specific OCR rules.", encoding="utf-8")
            register_project_pdf(
                project="replace_source",
                file=first_pdf,
                workspace_root=workspace_root,
            )

            result = replace_project_images(
                project="replace_source",
                folder=images,
                workspace_root=workspace_root,
            )

            self.assertEqual(result.source_type, "images")
            self.assertFalse(
                (location.path / "01_input/pdf/first.pdf").exists()
            )
            self.assertTrue(
                (location.path / "01_input/images/card.png").is_file()
            )
            self.assertEqual(
                prompt.read_text(encoding="utf-8"),
                "Project-specific OCR rules.",
            )
            self.assertEqual(
                load_project(
                    "replace_source",
                    workspace_root,
                ).manifest.source_file,
                "01_input/images",
            )

    def test_rejects_replacement_after_source_processing_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\nfirst\n")
            second_pdf.write_bytes(b"%PDF-1.4\nsecond\n")
            location = create_project(
                name="Started Source",
                workspace_root=workspace_root,
            )
            register_project_pdf(
                project="started_source",
                file=first_pdf,
                workspace_root=workspace_root,
            )
            (location.path / ".glk/state/pdf_acquisition.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SourceRegistrationError,
                "after extraction or OCR has started",
            ):
                replace_project_pdf(
                    project="started_source",
                    file=second_pdf,
                    workspace_root=workspace_root,
                )

            self.assertTrue(
                (location.path / "01_input/pdf/first.pdf").is_file()
            )
            self.assertFalse(
                (location.path / "01_input/pdf/second.pdf").exists()
            )

    def test_failed_replacement_restores_originals_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\nfirst\n")
            second_pdf.write_bytes(b"%PDF-1.4\nsecond\n")
            location = create_project(
                name="Rollback Source",
                workspace_root=workspace_root,
            )
            register_project_pdf(
                project="rollback_source",
                file=first_pdf,
                workspace_root=workspace_root,
            )

            with patch(
                "glk.application.source_registration_service.copy_file_atomic",
                side_effect=OSError("copy failed"),
            ):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    replace_project_pdf(
                        project="rollback_source",
                        file=second_pdf,
                        workspace_root=workspace_root,
                    )

            self.assertTrue(
                (location.path / "01_input/pdf/first.pdf").is_file()
            )
            self.assertFalse(
                (location.path / "01_input/pdf/second.pdf").exists()
            )
            self.assertTrue(
                (location.path / "01_input/images/ocr_prompt.txt").is_file()
            )
            self.assertEqual(
                load_project(
                    "rollback_source",
                    workspace_root,
                ).manifest.source_file,
                "01_input/pdf/first.pdf",
            )
