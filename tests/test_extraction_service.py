from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from glk.application.extraction_service import ExtractionError, extract_project_pdf
from glk.application.project_service import create_project, load_project
from glk.extraction.layout import PROMPT_VERSION, merge_paragraph_continuations


class FakeLayoutProvider:
    model_name = "fake-layout-model"
    prompt_version = PROMPT_VERSION

    def __init__(self, fail_if_called: bool = False) -> None:
        self.calls = 0
        self.fail_if_called = fail_if_called

    def reconstruct(
        self, page_number: int, fragments: list[dict[str, Any]], page_image: Image.Image
    ) -> dict[str, Any]:
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("Provider should not have been called")
        return {
            "blocks": [
                {
                    "type": "paragraph",
                    "fragment_ids": [fragment["id"] for fragment in fragments],
                    "include_in_text": True,
                    "reason": "",
                }
            ]
        }


class InvalidLayoutProvider(FakeLayoutProvider):
    def reconstruct(
        self, page_number: int, fragments: list[dict[str, Any]], page_image: Image.Image
    ) -> dict[str, Any]:
        self.calls += 1
        return {"blocks": []}


class RecoveringLayoutProvider(FakeLayoutProvider):
    def reconstruct(
        self, page_number: int, fragments: list[dict[str, Any]], page_image: Image.Image
    ) -> dict[str, Any]:
        if self.calls == 0:
            self.calls += 1
            return {"blocks": []}
        return super().reconstruct(page_number, fragments, page_image)


def create_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


class ExtractionServiceTests(unittest.TestCase):
    def test_merges_lowercase_paragraph_continuation(self) -> None:
        blocks = [
            {
                "type": "paragraph",
                "fragment_ids": ["P001-F001"],
                "include_in_text": True,
                "reason": "",
                "text": "She stares at the",
            },
            {
                "type": "paragraph",
                "fragment_ids": ["P001-F002"],
                "include_in_text": True,
                "reason": "",
                "text": "sky. The dragon appears.",
            },
        ]
        merged = merge_paragraph_continuations(blocks)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "She stares at the sky. The dragon appears.")
        self.assertEqual(merged[0]["fragment_ids"], ["P001-F001", "P001-F002"])

    def test_extracts_pdf_and_reuses_validated_page_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            pdf_path = root / "rulebook.pdf"
            create_pdf(pdf_path, "A wrapped rulebook sentence.")
            create_project(name="Rulebook", workspace_root=workspace_root)

            provider = FakeLayoutProvider()
            first = extract_project_pdf(
                project="rulebook",
                file=pdf_path,
                workspace_root=workspace_root,
                provider=provider,
            )
            self.assertTrue(first.ok)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(first.cached_pages, ())
            project_path = workspace_root / "rulebook"
            self.assertIn("A wrapped rulebook sentence.", Path(first.output_file).read_text())
            self.assertEqual(
                load_project("rulebook", workspace_root).manifest.source_file,
                "01_input/pdf/rulebook.pdf",
            )
            self.assertTrue(
                (project_path / "01_input/pdf/rulebook.pdf").is_file()
            )
            self.assertFalse((project_path / "02_source/assets").exists())

            cached_provider = FakeLayoutProvider(fail_if_called=True)
            second = extract_project_pdf(
                project="rulebook",
                workspace_root=workspace_root,
                provider=cached_provider,
            )
            self.assertTrue(second.ok)
            self.assertEqual(cached_provider.calls, 0)
            self.assertEqual(second.cached_pages, (1,))
            metadata = json.loads((project_path / ".glk/state/pdf_acquisition.json").read_text())
            self.assertEqual(metadata["status"], "complete")

    def test_uses_pdf_already_in_input_without_making_a_source_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Input PDF", workspace_root=workspace_root)
            pdf_path = location.path / "01_input/pdf/rulebook.pdf"
            create_pdf(pdf_path, "Use this file directly.")

            result = extract_project_pdf(
                project="input_pdf",
                file=pdf_path,
                workspace_root=workspace_root,
                provider=FakeLayoutProvider(),
            )

            self.assertEqual(Path(result.source_pdf), pdf_path.resolve())
            self.assertEqual(
                load_project("input_pdf", workspace_root).manifest.source_file,
                "01_input/pdf/rulebook.pdf",
            )
            self.assertEqual(
                [
                    path.name
                    for path in location.path.joinpath("01_input/pdf").glob("*.pdf")
                ],
                ["rulebook.pdf"],
            )
            self.assertFalse((location.path / "02_source/assets").exists())

    def test_different_registered_source_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            create_pdf(first_pdf, "First source")
            create_pdf(second_pdf, "Second source")
            create_project(name="Rulebook", workspace_root=workspace_root)
            extract_project_pdf(
                project="rulebook",
                file=first_pdf,
                workspace_root=workspace_root,
                provider=FakeLayoutProvider(),
            )
            with self.assertRaises(ExtractionError):
                extract_project_pdf(
                    project="rulebook",
                    file=second_pdf,
                    workspace_root=workspace_root,
                    provider=FakeLayoutProvider(),
                )

    def test_invalid_layout_is_partial_and_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            pdf_path = root / "rulebook.pdf"
            create_pdf(pdf_path, "Source text must not disappear.")
            create_project(name="Rulebook", workspace_root=workspace_root)
            provider = InvalidLayoutProvider()
            result = extract_project_pdf(
                project="rulebook",
                file=pdf_path,
                workspace_root=workspace_root,
                provider=provider,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.successful_pages, ())
            self.assertIn("fragment validation", result.failures[0].error)
            self.assertEqual(
                result.failures[0].code,
                "GEMINI_RESPONSE_INVALID",
            )
            self.assertEqual(provider.calls, 3)
            self.assertFalse(
                (workspace_root / "rulebook/.glk/cache/pdf/layouts/page_001.json").exists()
            )

    def test_retries_fragment_validation_failure_and_caches_recovered_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_root = root / "workspaces"
            pdf_path = root / "rulebook.pdf"
            create_pdf(pdf_path, "Retry a missing fragment response.")
            create_project(name="Rulebook", workspace_root=workspace_root)
            provider = RecoveringLayoutProvider()
            progress: list[str] = []

            result = extract_project_pdf(
                project="rulebook",
                file=pdf_path,
                workspace_root=workspace_root,
                provider=provider,
                progress=progress.append,
            )

            self.assertTrue(result.ok)
            self.assertEqual(provider.calls, 2)
            self.assertTrue(
                any(
                    "fragment validation failed" in message
                    and "(2/3)" in message
                    for message in progress
                )
            )
            self.assertTrue(
                (workspace_root / "rulebook/.glk/cache/pdf/layouts/page_001.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
