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
    get_project_source_review_document,
    prepare_project_source_review,
    save_project_source_review,
)
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock


def make_block(
    order: int,
    text: str,
    *,
    page: int | None = 1,
    warnings: tuple[str, ...] = (),
) -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"pdf-p0001-b{order:04d}-{order:010d}",
        source_type="pdf",
        source_file="01_input/pdf/rulebook.pdf",
        page=page,
        source_order=order,
        block_order=order,
        block_type="body",
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility=None,
        status="flagged" if warnings else "raw",
        warnings=warnings,
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
        write_blocks(location.path / ".glk/segments/source.jsonl", blocks)
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
            draft_path = project_path / "02_source/draft.txt"
            review_path = project_path / "02_source/review.txt"
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
            review_path = project_path / "02_source/review.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace("l0", "10"),
                encoding="utf-8",
            )

            result = finalize_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertEqual(result.changed_blocks, 1)
            approved = read_blocks(project_path / ".glk/segments/approved_source.jsonl")
            self.assertEqual(approved[0].raw_text, "Increase HP by l0.")
            self.assertEqual(approved[0].corrected_text, "Increase HP by 10.")
            self.assertEqual(approved[1].corrected_text, None)
            self.assertTrue(all(block.status == "approved" for block in approved))
            self.assertIn("Increase HP by 10.", (project_path / "02_source/final.txt").read_text())

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
                project_path / ".glk/segments/source.jsonl",
                [make_block(1, "New extraction.")],
            )

            stale = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            self.assertEqual(stale.review_status, "stale")
            self.assertIn("New extraction.", (project_path / "02_source/draft.txt").read_text())
            self.assertIn("First extraction.", (project_path / "02_source/review.txt").read_text())
            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )

            reset = prepare_project_source_review(
                project="review_project", workspace_root=workspace_root, force=True
            )
            self.assertEqual(reset.review_status, "current")
            self.assertIn("New extraction.", (project_path / "02_source/review.txt").read_text())

    def test_finalize_rejects_marker_damage_and_unresolved_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Readable text.")]
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "02_source/review.txt"
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

    def test_finalize_can_explicitly_preserve_unresolved_icon_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root,
                [make_block(1, "Skill [ICON: orange diamond].")],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )

            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project", workspace_root=workspace_root
                )

            result = finalize_project_source_review(
                project="review_project",
                workspace_root=workspace_root,
                allow_unresolved_icons=True,
            )

            self.assertTrue(result.unresolved_icons_allowed)
            self.assertEqual(result.unresolved_icon_blocks, 1)
            self.assertIn(
                "[ICON: orange diamond]",
                (project_path / "02_source/final.txt").read_text(encoding="utf-8"),
            )
            state = json.loads(
                (project_path / ".glk/state/source_review.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(state["unresolved_icons_allowed"])
            self.assertEqual(
                state["unresolved_icon_block_ids"],
                ["pdf-p0001-b0001-0000000001"],
            )

    def test_unresolved_icon_override_does_not_allow_illegible_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            self.create_source(
                workspace_root,
                [make_block(1, "Damage [ILLEGIBLE].")],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )

            with self.assertRaises(SourceReviewError):
                finalize_project_source_review(
                    project="review_project",
                    workspace_root=workspace_root,
                    allow_unresolved_icons=True,
                )

    def test_token_change_requires_explicit_option_and_accepts_windows_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Gain [HP].")]
            )
            (project_path / "01_input/images/ocr_prompt.txt").write_text(
                "- [HP]: heart.\n- [DEF]: shield.\n", encoding="utf-8"
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "02_source/review.txt"
            edited = review_path.read_text(encoding="utf-8").replace("[HP]", "[DEF]")
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
            approved = read_blocks(project_path / ".glk/segments/approved_source.jsonl")
            self.assertEqual(approved[0].corrected_text, "Gain [DEF].")

    def test_browser_document_exposes_source_warnings_without_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            warning = "줄바꿈 하이픈 결합 확인: multi- + player → multiplayer"
            self.create_source(
                workspace_root,
                [make_block(1, "multiplayer rule.", warnings=(warning,))],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )

            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )

            self.assertEqual(document["blocks"][0]["warnings"], [warning])
            self.assertEqual(document["summary"]["warnings"], 1)
            self.assertEqual(document["summary"]["issues"], 0)

    def test_browser_document_counts_layout_recovery_warnings_by_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            warning = (
                "AI 레이아웃 정렬 누락 복구: P001-F002 — "
                "원본 이미지에서 위치와 순서를 확인하세요."
            )
            self.create_source(
                workspace_root,
                [make_block(1, "Recovered source.", warnings=(warning,))],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )

            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )

            self.assertEqual(document["groups"][0]["layout_warnings"], 1)
            self.assertEqual(document["blocks"][0]["layout_warnings"], 1)
            self.assertEqual(document["summary"]["layout_warnings"], 1)

    def test_browser_document_does_not_duplicate_source_warning_as_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            warning = "줄바꿈 하이픈 결합 확인: multi- + player → multiplayer"
            project_path = self.create_source(
                workspace_root,
                [make_block(1, "multiplayer rule.", warnings=(warning,))],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            write_json_path = project_path / ".glk/reports/source_qa.json"
            write_json_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_path.write_text(
                json.dumps(
                    {
                        "issues": [
                            {
                                "block_id": "pdf-p0001-b0001-0000000001",
                                "code": "SOURCE_WARNING",
                                "message": "generic source warning",
                            },
                            {
                                "block_id": "pdf-p0001-b0001-0000000001",
                                "code": "TOKEN_UNKNOWN",
                                "message": "specific QA warning",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )

            self.assertEqual(document["blocks"][0]["warnings"], [warning])
            self.assertEqual(
                [issue["code"] for issue in document["blocks"][0]["issues"]],
                ["TOKEN_UNKNOWN"],
            )
            self.assertEqual(document["summary"]["warnings"], 1)
            self.assertEqual(document["summary"]["issues"], 1)

    def test_browser_save_reorders_excludes_and_adds_manual_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root,
                [
                    make_block(1, "First."),
                    make_block(2, "Second."),
                    make_block(3, "Remove me."),
                ],
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )
            by_id = {block["id"]: block for block in document["blocks"]}
            first = by_id[make_block(1, "First.").id]
            second = by_id[make_block(2, "Second.").id]
            removed = by_id[make_block(3, "Remove me.").id]
            payload = [
                {**second, "text": "Second corrected."},
                {
                    "id": "new-1",
                    "text": "Missing paragraph.",
                    "excluded": False,
                    "source_type": "pdf",
                    "source_file": "01_input/pdf/rulebook.pdf",
                    "page": 1,
                    "bbox": [120, 300, 880, 420],
                },
                first,
                {**removed, "excluded": True},
            ]

            saved = save_project_source_review(
                project="review_project",
                workspace_root=workspace_root,
                blocks=payload,
                expected_review_sha256=document["review_sha256"],
            )
            self.assertEqual(saved["summary"]["manual"], 1)
            self.assertEqual(saved["summary"]["excluded"], 1)
            self.assertEqual(
                [block["text"] for block in saved["blocks"] if not block["excluded"]],
                ["Second corrected.", "Missing paragraph.", "First."],
            )

            finalize_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            approved = read_blocks(
                project_path / ".glk/segments/approved_source.jsonl"
            )
            self.assertEqual(
                [block.effective_text for block in approved],
                ["Second corrected.", "Missing paragraph.", "First."],
            )
            self.assertTrue(approved[1].id.startswith("manual-"))
            self.assertEqual(approved[1].bbox, (120.0, 300.0, 880.0, 420.0))
            self.assertEqual([block.source_order for block in approved], [1, 2, 3])
            self.assertEqual([block.block_order for block in approved], [1, 2, 3])

    def test_browser_save_rejects_missing_original_and_manual_bbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            self.create_source(
                workspace_root, [make_block(1, "One."), make_block(2, "Two.")]
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )
            with self.assertRaisesRegex(SourceReviewError, "Every extracted block"):
                save_project_source_review(
                    project="review_project",
                    workspace_root=workspace_root,
                    blocks=[document["blocks"][0]],
                    expected_review_sha256=document["review_sha256"],
                )
            with self.assertRaisesRegex(SourceReviewError, "bbox"):
                save_project_source_review(
                    project="review_project",
                    workspace_root=workspace_root,
                    blocks=[
                        *document["blocks"],
                        {
                            "id": "new-1",
                            "text": "Missing.",
                            "excluded": False,
                            "source_type": "pdf",
                            "source_file": "01_input/pdf/rulebook.pdf",
                            "page": 1,
                            "bbox": None,
                        },
                    ],
                    expected_review_sha256=document["review_sha256"],
                )
            with self.assertRaisesRegex(SourceReviewError, "source file"):
                save_project_source_review(
                    project="review_project",
                    workspace_root=workspace_root,
                    blocks=[
                        *document["blocks"],
                        {
                            "id": "new-2",
                            "text": "Wrong source.",
                            "excluded": False,
                            "source_type": "pdf",
                            "source_file": "01_input/pdf/other.pdf",
                            "page": 1,
                            "bbox": [100, 100, 200, 200],
                        },
                    ],
                    expected_review_sha256=document["review_sha256"],
                )

    def test_version_one_review_opens_and_first_browser_save_upgrades_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = self.create_source(
                workspace_root, [make_block(1, "Legacy review.")]
            )
            prepare_project_source_review(
                project="review_project", workspace_root=workspace_root
            )
            review_path = project_path / "02_source/review.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "[[GLK_REVIEW version=2]]", "[[GLK_REVIEW version=1]]"
                ),
                encoding="utf-8",
            )
            state_path = project_path / ".glk/state/source_review.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["format_version"] = 1
            state.pop("ordered_block_ids")
            state.pop("excluded_block_ids")
            state.pop("manual_blocks")
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            document = get_project_source_review_document(
                project="review_project", workspace_root=workspace_root
            )
            save_project_source_review(
                project="review_project",
                workspace_root=workspace_root,
                blocks=document["blocks"],
                expected_review_sha256=document["review_sha256"],
            )

            self.assertTrue(
                review_path.read_text(encoding="utf-8").startswith(
                    "[[GLK_REVIEW version=2]]"
                )
            )
            upgraded = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["format_version"], 2)


if __name__ == "__main__":
    unittest.main()
