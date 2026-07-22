from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from glk.application.image_ocr_service import ocr_project_images
from glk.application.project_service import create_project, load_project


class FakeImageOcrProvider:
    model_name = "fake-ocr"
    prompt_version = "test-prompt-v1"

    def __init__(self, *, fail_if_called: bool = False) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self.fail_if_called = fail_if_called

    def transcribe(self, prompt: str, image: Image.Image) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail_if_called:
            raise AssertionError("Provider should not have been called")
        self.assert_rgb_image(image)
        return {
            "blocks": [
                {
                    "type": "body",
                    "text": "Deal 1{DMGR}.",
                    "bbox": [10, 20, 900, 800],
                    "legibility": "clear",
                }
            ],
            "warnings": [],
        }

    @staticmethod
    def assert_rgb_image(image: Image.Image) -> None:
        if image.mode != "RGB":
            raise AssertionError(f"Expected RGB image, got {image.mode}")


class ImageOcrServiceTests(unittest.TestCase):
    def test_registers_images_writes_outputs_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            image_folder = root / "images"
            image_folder.mkdir()
            Image.new("RGB", (20, 10), "white").save(image_folder / "card-1.png")
            (image_folder / "ocr_prompt.txt").write_text(
                "Eight-point burst is {DMGR}.", encoding="utf-8"
            )
            (image_folder / "card-1.png.prompt.txt").write_text(
                "Read the footer.", encoding="utf-8"
            )
            create_project(name="Card Set", workspace_root=workspace_root)

            provider = FakeImageOcrProvider()
            first = ocr_project_images(
                project="card_set",
                folder=image_folder,
                workspace_root=workspace_root,
                provider=provider,
            )
            self.assertTrue(first.ok)
            self.assertEqual(provider.calls, 1)
            self.assertIn("Eight-point burst is {DMGR}.", provider.prompts[0])
            self.assertIn("Read the footer.", provider.prompts[0])
            project_path = workspace_root / "card_set"
            self.assertEqual(
                load_project("card_set", workspace_root).manifest.source_file,
                "source/images",
            )
            self.assertTrue((project_path / "source/images/card-1.png").is_file())
            self.assertEqual(
                (project_path / "source/ocr/individual/card-1.txt").read_text().strip(),
                "Deal 1{DMGR}.",
            )
            self.assertEqual(
                (project_path / "source/ocr/combined.txt").read_text().strip(),
                "[card-1.txt]\nDeal 1{DMGR}.\n\n======================",
            )

            cached_provider = FakeImageOcrProvider(fail_if_called=True)
            second = ocr_project_images(
                project="card_set",
                workspace_root=workspace_root,
                provider=cached_provider,
            )
            self.assertTrue(second.ok)
            self.assertEqual(cached_provider.calls, 0)
            self.assertEqual(second.cached_images, ("card-1.png",))

    def test_dry_run_does_not_require_provider_or_write_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            image_folder = root / "images"
            image_folder.mkdir()
            Image.new("RGB", (10, 10), "black").save(image_folder / "sample.jpg")
            create_project(name="Dry Cards", workspace_root=workspace_root)

            result = ocr_project_images(
                project="dry_cards",
                folder=image_folder,
                workspace_root=workspace_root,
                dry_run=True,
            )
            self.assertTrue(result.dry_run)
            self.assertEqual(result.selected_images, ("sample.jpg",))
            self.assertFalse(
                (workspace_root / "dry_cards/source/images/sample.jpg").exists()
            )


if __name__ == "__main__":
    unittest.main()
