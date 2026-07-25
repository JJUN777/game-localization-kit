from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from glk.application.dashboard_service import get_dashboard_document
from glk.application.project_service import create_project
from glk.application.source_registration_service import (
    register_project_images,
    register_project_pdf,
)


class DashboardServiceTests(unittest.TestCase):
    def test_empty_workspace_returns_a_stable_empty_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"

            document = get_dashboard_document(workspace_root)

            self.assertTrue(document["ok"])
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["projects"], [])
            self.assertEqual(
                document["summary"],
                {
                    "projects": 0,
                    "in_progress": 0,
                    "completed": 0,
                    "needs_attention": 0,
                },
            )

    def test_new_project_is_present_with_reviews_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            create_project(name="Dashboard Game", workspace_root=workspace_root)

            document = get_dashboard_document(workspace_root)

            self.assertEqual(document["summary"]["projects"], 1)
            self.assertEqual(document["summary"]["in_progress"], 0)
            project = document["projects"][0]
            self.assertEqual(project["project_id"], "dashboard_game")
            self.assertEqual(project["stage"], "not_started")
            self.assertEqual(project["stage_label"], "시작 전")
            self.assertEqual(project["progress"], 0)
            self.assertTrue(project["workspace_ready"])
            self.assertFalse(project["reviews"]["source"]["enabled"])
            self.assertFalse(project["reviews"]["glossary"]["enabled"])
            self.assertFalse(project["reviews"]["translation"]["enabled"])
            self.assertIn("프로젝트 공통 OCR", project["ocr_prompt"])
            self.assertFalse(project["translation_prompt"]["saved"])
            self.assertIn(
                "natural Korean",
                project["translation_prompt"]["value"],
            )

    def test_source_replacement_is_hidden_after_processing_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_pdf = root / "rulebook.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\nsource\n")
            location = create_project(
                name="Replaceable Source",
                workspace_root=workspace_root,
            )
            register_project_pdf(
                project="replaceable_source",
                file=source_pdf,
                workspace_root=workspace_root,
            )

            before = get_dashboard_document(workspace_root)["projects"][0]
            self.assertTrue(before["source_replacement"]["allowed"])
            self.assertEqual(before["source_files"], ["rulebook.pdf"])

            (location.path / ".glk/state/pdf_acquisition.json").write_text(
                "{}",
                encoding="utf-8",
            )
            after = get_dashboard_document(workspace_root)["projects"][0]
            self.assertFalse(after["source_replacement"]["allowed"])
            self.assertIn("시작되어", after["source_replacement"]["reason"])

    def test_image_source_files_use_relative_paths_and_natural_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            source_images = root / "images"
            nested = source_images / "cards"
            nested.mkdir(parents=True)
            (nested / "card-10.png").write_bytes(b"image 10")
            (nested / "card-2.png").write_bytes(b"image 2")
            create_project(
                name="Image Files",
                workspace_root=workspace_root,
            )
            register_project_images(
                project="image_files",
                folder=source_images,
                workspace_root=workspace_root,
            )

            project = get_dashboard_document(workspace_root)["projects"][0]

            self.assertEqual(
                project["source_files"],
                ["cards/card-2.png", "cards/card-10.png"],
            )
            self.assertIn("프로젝트 공통 OCR", project["ocr_prompt"])
            self.assertTrue(project["ocr_prompt_edit"]["allowed"])

            (workspace_root / "image_files/.glk/state/image_ocr.json").write_text(
                "{}",
                encoding="utf-8",
            )
            after = get_dashboard_document(workspace_root)["projects"][0]
            self.assertFalse(after["ocr_prompt_edit"]["allowed"])
            self.assertIn("시작되어", after["ocr_prompt_edit"]["reason"])


if __name__ == "__main__":
    unittest.main()
