from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from glk.application.project_service import create_project
from glk.application.source_review_service import (
    SourceReviewError,
    finalize_project_source_review,
    prepare_project_source_review,
)
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock


def make_block(order: int, text: str, *, page: int | None = 1) -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"pdf-p0001-b{order:04d}-{order:010d}",
        source_type="pdf",
        source_file="source/original.pdf",
        page=page,
        source_order=order,
        block_order=order,
        block_type="body",
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility=None,
        status="raw",
        warnings=(),
        source_refs=(f"P001-F{order:03d}",),
        source_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def write_blocks(path: Path, blocks: list[SourceBlock]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for block in blocks
        ),
        encoding="utf-8",
    )


def read_blocks(path: Path) -> list[SourceBlock]:
    return [
        SourceBlock.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class SourceReviewServiceTests(unittest.TestCase):
    def create_source(self, workspace_root: Path, blocks: list[SourceBlock]) -> Path:
        location = create_project(name="Review Project", workspace_root=workspace_root)
        write_blocks(location.path / "segments/source.jsonl", blocks)
        return location.path

    def test_prepare_creates_identical_files_and_preserves_edited_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Original text.")]
            )

            first = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertTrue(first.review_created)
            draft_path = project_path / "draft/source.txt"
            review_path = project_path / "review/source.txt"
            self.assertEqual(draft_path.read_bytes(), review_path.read_bytes())

            edited = review_path.read_text(encoding="utf-8").replace(
                "Original text.", "Human correction."
            )
            review_path.write_text(edited, encoding="utf-8")
            second = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertFalse(second.review_created)
            self.assertEqual(second.review_status, "current")
            self.assertIn("Human correction.", review_path.read_text(encoding="utf-8"))

    def test_finalize_preserves_raw_text_and_writes_corrected_approved_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root,
                [make_block(1, "Increase HP by l0."), make_block(2, "Keep this.")],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "review/source.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace("l0", "10"),
                encoding="utf-8",
            )

            result = finalize_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertEqual(result.changed_blocks, 1)
            approved = read_blocks(project_path / "segments/approved_source.jsonl")
            self.assertEqual(approved[0].raw_text, "Increase HP by l0.")
            self.assertEqual(approved[0].corrected_text, "Increase HP by 10.")
            self.assertEqual(approved[1].corrected_text, None)
            self.assertTrue(all(block.status == "approved" for block in approved))
            self.assertIn("Increase HP by 10.", (project_path / "final/source.txt").read_text())

    def test_source_change_marks_existing_review_stale_until_forced_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "First extraction.")]
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            write_blocks(
                project_path / "segments/source.jsonl",
                [make_block(1, "New extraction.")],
            )

            stale = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertEqual(stale.review_status, "stale")
            self.assertIn("New extraction.", (project_path / "draft/source.txt").read_text())
            self.assertIn("First extraction.", (project_path / "review/source.txt").read_text())
            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )

            reset = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root, force=True
            )
            self.assertEqual(reset.review_status, "current")
            self.assertIn("New extraction.", (project_path / "review/source.txt").read_text())

    def test_finalize_rejects_marker_damage_and_unresolved_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Readable text.")]
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "review/source.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace("[PAGE 1]", "[PAGE 2]"),
                encoding="utf-8",
            )
            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )

            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root, force=True
            )
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "Readable text.", "[ILLEGIBLE]"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )

    def test_token_change_requires_explicit_option_and_accepts_windows_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Gain {HP}.")]
            )
            (project_path / "source/ocr_prompt.txt").write_text(
                "Heart: {HP}\nShield: {DEF}\n", encoding="utf-8"
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "review/source.txt"
            edited = review_path.read_text(encoding="utf-8").replace("{HP}", "{DEF}")
            review_path.write_bytes(edited.replace("\n", "\r\n").encode("utf-8"))

            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )
            result = finalize_project_source_review(
                project="review_project",
                workspace_root=workspace_root,
                allow_token_changes=True,
            )
            self.assertEqual(result.changed_blocks, 1)
            approved = read_blocks(project_path / "segments/approved_source.jsonl")
            self.assertEqual(approved[0].corrected_text, "Gain {DEF}.")


if __name__ == "__main__":
    unittest.main()
