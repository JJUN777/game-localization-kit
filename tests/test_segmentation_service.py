from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from glk.application.project_service import create_project, update_project_source
from glk.application.segmentation_service import (
    SegmentationError,
    segment_project_source,
)
from glk.domain.source_block import SourceBlock


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def create_pdf_source(workspace_root: Path) -> Path:
    location = create_project(name="PDF Source", workspace_root=workspace_root)
    update_project_source(location, "01_input/pdf/rulebook.pdf")
    project_path = location.path
    write_json(
        project_path / ".glk/state/pdf_acquisition.json",
        {
            "status": "complete",
            "successful_pages": [1],
            "failures": [],
        },
    )
    write_json(
        project_path / ".glk/cache/pdf/fragments/page_001.json",
        {
            "page_size": [100, 200],
            "fragments": [
                {"id": "P001-F001", "bbox": [10, 20, 50, 40], "text": "Title"},
                {"id": "P001-F002", "bbox": [10, 50, 90, 80], "text": "Body"},
                {"id": "P001-F003", "bbox": [45, 180, 55, 190], "text": "1"},
            ],
        },
    )
    write_json(
        project_path / ".glk/cache/pdf/layouts/page_001.json",
        {
            "reconstructed_blocks": [
                {
                    "type": "heading",
                    "fragment_ids": ["P001-F001"],
                    "include_in_text": True,
                    "text": "Title",
                },
                {
                    "type": "paragraph",
                    "fragment_ids": ["P001-F002"],
                    "include_in_text": True,
                    "text": "Body text.",
                },
                {
                    "type": "page_number",
                    "fragment_ids": ["P001-F003"],
                    "include_in_text": False,
                    "text": "1",
                },
            ]
        },
    )
    return project_path


def create_image_source(workspace_root: Path, *, status: str = "complete") -> Path:
    location = create_project(name="Image Source", workspace_root=workspace_root)
    update_project_source(location, "01_input/images")
    project_path = location.path
    write_json(
        project_path / ".glk/state/image_ocr.json",
        {
            "status": status,
            "successful_images": ["characters/card-2.jpg"],
            "failures": [] if status == "complete" else [{"file": "bad.jpg"}],
        },
    )
    write_json(
        project_path / ".glk/cache/ocr/results/characters/card-2.json",
        {
            "source_image": "01_input/images/characters/card-2.jpg",
            "ocr": {
                "blocks": [
                    {
                        "type": "title",
                        "text": "CARD TITLE",
                        "bbox": [100, 50, 900, 150],
                        "legibility": "clear",
                    },
                    {
                        "type": "body",
                        "text": "[ILLEGIBLE]",
                        "bbox": [100, 300, 900, 700],
                        "legibility": "uncertain",
                    },
                ],
                "warnings": [],
            },
        },
    )
    return project_path


def read_blocks(path: Path) -> list[SourceBlock]:
    return [
        SourceBlock.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class SegmentationServiceTests(unittest.TestCase):
    def test_rebuilds_v3_cache_once_before_reusing_v4_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_pdf_source(workspace_root)
            with patch(
                "glk.application.segmentation_service.SEGMENTATION_VERSION",
                "source-block-v3",
            ):
                previous = segment_project_source(
                    project="pdf_source", workspace_root=workspace_root
                )
            self.assertFalse(previous.cached)

            rebuilt = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )
            cached = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )

            self.assertFalse(rebuilt.cached)
            self.assertTrue(cached.cached)
            state = json.loads(
                (project_path / ".glk/state/segmentation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["version"], "source-block-v4")

    def test_normalizes_pdf_blocks_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_pdf_source(workspace_root)

            first = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )
            self.assertEqual(first.total_blocks, 2)
            self.assertEqual(first.flagged_blocks, 0)
            blocks = read_blocks(project_path / ".glk/segments/source.jsonl")
            self.assertEqual([block.raw_text for block in blocks], ["Title", "Body text."])
            self.assertEqual(
                (project_path / "02_source/draft.txt").read_bytes(),
                (project_path / "02_source/review.txt").read_bytes(),
            )
            self.assertTrue(first.review_created)
            self.assertEqual(blocks[0].bbox, (100.0, 100.0, 500.0, 200.0))
            self.assertEqual(blocks[1].source_refs, ("P001-F002",))
            first_ids = [block.id for block in blocks]

            cached = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )
            self.assertTrue(cached.cached)

            document_path = project_path / ".glk/state/pdf_acquisition.json"
            document = json.loads(document_path.read_text(encoding="utf-8"))
            document["updated_at"] = "2099-01-01T00:00:00Z"
            document["cached_pages"] = [1]
            write_json(document_path, document)
            volatile_metadata_change = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )
            self.assertTrue(volatile_metadata_change.cached)

            layout_path = project_path / ".glk/cache/pdf/layouts/page_001.json"
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            layout["reconstructed_blocks"][1]["text"] = "Changed body text."
            write_json(layout_path, layout)
            changed = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )
            self.assertFalse(changed.cached)
            self.assertEqual(changed.review_status, "stale")
            changed_blocks = read_blocks(project_path / ".glk/segments/source.jsonl")
            self.assertEqual([block.id for block in changed_blocks], first_ids)
            self.assertNotEqual(changed_blocks[1].source_hash, blocks[1].source_hash)

    def test_normalizes_nested_image_blocks_and_flags_uncertain_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_image_source(workspace_root)
            result = segment_project_source(
                project="image_source", workspace_root=workspace_root
            )
            self.assertEqual(result.total_blocks, 2)
            self.assertEqual(result.flagged_blocks, 1)
            blocks = read_blocks(project_path / ".glk/segments/source.jsonl")
            self.assertEqual(
                blocks[0].source_file,
                "01_input/images/characters/card-2.jpg",
            )
            self.assertEqual(blocks[0].bbox, (100.0, 50.0, 900.0, 150.0))
            self.assertEqual(blocks[0].status, "raw")
            self.assertEqual(blocks[1].status, "flagged")
            self.assertIsNone(blocks[1].page)

    def test_flags_pdf_line_wrap_hyphen_join_with_fragment_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_pdf_source(workspace_root)
            fragment_path = (
                project_path / ".glk/cache/pdf/fragments/page_001.json"
            )
            fragment_data = json.loads(fragment_path.read_text(encoding="utf-8"))
            fragment_data["fragments"][1:2] = [
                {
                    "id": "P001-F002",
                    "bbox": [10, 50, 90, 65],
                    "text": "A multi-",
                },
                {
                    "id": "P001-F004",
                    "bbox": [10, 66, 90, 80],
                    "text": "player rule.",
                },
            ]
            write_json(fragment_path, fragment_data)
            layout_path = project_path / ".glk/cache/pdf/layouts/page_001.json"
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            layout["reconstructed_blocks"][1].update(
                {
                    "fragment_ids": ["P001-F002", "P001-F004"],
                    "text": "A multiplayer rule.",
                }
            )
            write_json(layout_path, layout)

            result = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )

            self.assertEqual(result.flagged_blocks, 1)
            blocks = read_blocks(project_path / ".glk/segments/source.jsonl")
            self.assertEqual(blocks[1].status, "flagged")
            self.assertEqual(
                blocks[1].warnings,
                (
                    "줄바꿈 하이픈 결합 확인: multi- + player → multiplayer",
                ),
            )

    def test_preserves_pdf_layout_recovery_warning_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_pdf_source(workspace_root)
            layout_path = project_path / ".glk/cache/pdf/layouts/page_001.json"
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            warning = (
                "AI 레이아웃 정렬 누락 복구: P001-F002 — "
                "원본 이미지에서 위치와 순서를 확인하세요."
            )
            layout["reconstructed_blocks"][1]["warnings"] = [warning]
            write_json(layout_path, layout)

            result = segment_project_source(
                project="pdf_source", workspace_root=workspace_root
            )

            self.assertEqual(result.flagged_blocks, 1)
            blocks = read_blocks(project_path / ".glk/segments/source.jsonl")
            self.assertEqual(blocks[1].status, "flagged")
            self.assertEqual(blocks[1].warnings, (warning,))

    def test_rejects_partial_source_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            create_image_source(workspace_root, status="partial")
            with self.assertRaises(SegmentationError):
                segment_project_source(
                    project="image_source", workspace_root=workspace_root
                )

    def test_dry_run_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_pdf_source(workspace_root)
            result = segment_project_source(
                project="pdf_source", workspace_root=workspace_root, dry_run=True
            )
            self.assertTrue(result.dry_run)
            self.assertEqual(result.total_blocks, 2)
            self.assertFalse((project_path / ".glk/segments/source.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
