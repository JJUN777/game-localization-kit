from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from glk.application.project_service import create_project
from glk.application.source_qa_service import (
    run_local_source_qa,
    run_project_source_qa,
)
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock


def make_block(
    order: int,
    text: str,
    *,
    block_type: str = "body",
    legibility: str | None = "clear",
    status: str = "raw",
    warnings: tuple[str, ...] = (),
    source_hash: str | None = None,
) -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"image-card-b{order:04d}-{order:010d}",
        source_type="image",
        source_file="01_input/images/card.jpg",
        page=None,
        source_order=order,
        block_order=order,
        block_type=block_type,
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility=legibility,
        status=status,
        warnings=warnings,
        source_refs=(),
        source_hash=(
            source_hash
            or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        ),
    )


def write_blocks(path: Path, blocks: list[SourceBlock]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(block.to_dict(), ensure_ascii=False) + "\n" for block in blocks
        ),
        encoding="utf-8",
    )


class LocalSourceQaRuleTests(unittest.TestCase):
    def test_clean_text_has_no_issues(self) -> None:
        blocks = [
            make_block(1, "Deal 10 {DMGR}."),
            make_block(2, "DE-CB-M43/90", block_type="identifier"),
            make_block(3, "{Dark}", block_type="identifier"),
        ]
        self.assertEqual(run_local_source_qa(blocks, ("DMGR", "Dark")), [])

    def test_flags_only_deterministic_suspicious_patterns(self) -> None:
        blocks = [
            make_block(1, "Deal l0 {DMR} and [ICON: star]."),
            make_block(
                2,
                "DE C8 M43/9O",
                block_type="identifier",
                legibility="uncertain",
                status="flagged",
                warnings=("Small text",),
            ),
            make_block(3, "Bad {DMGR �"),
            make_block(4, "[ILLEGIBLE]", legibility="uncertain", status="flagged"),
            make_block(5, "DUP-01", block_type="identifier"),
            make_block(6, "DUP-01", block_type="identifier"),
            make_block(7, "Hash mismatch", source_hash="sha256:" + "0" * 64),
        ]
        issues = run_local_source_qa(blocks, ("DMGR",))
        codes = {issue.code for issue in issues}
        self.assertTrue(
            {
                "TOKEN_UNKNOWN",
                "ICON_UNRESOLVED",
                "OCR_ALNUM_CONFUSION",
                "OCR_UNCERTAIN",
                "SOURCE_WARNING",
                "IDENTIFIER_FORMAT",
                "TOKEN_MALFORMED",
                "UNICODE_REPLACEMENT",
                "OCR_ILLEGIBLE",
                "IDENTIFIER_DUPLICATE",
                "SOURCE_HASH_MISMATCH",
            }.issubset(codes)
        )
        self.assertTrue(all(issue.auto_fixable is False for issue in issues))
        self.assertTrue(
            all(
                not any(
                    phrase in issue.message
                    for phrase in (
                        "contains",
                        "marked this",
                        "appears",
                        "does not match",
                    )
                )
                for issue in issues
            )
        )


class SourceQaServiceTests(unittest.TestCase):
    def test_writes_report_reuses_cache_and_tracks_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            location = create_project(name="QA Project", workspace_root=workspace_root)
            blocks = [make_block(1, "Deal 10 {DMGR}.")]
            write_blocks(location.path / ".glk/segments/source.jsonl", blocks)
            prompt_path = location.path / "01_input/images/ocr_prompt.txt"
            prompt_path.write_text("Eight-point burst: {DMGR}", encoding="utf-8")

            first = run_project_source_qa(
                project="qa_project", workspace_root=workspace_root
            )
            self.assertEqual(first.total_issues, 0)
            self.assertEqual(first.allowed_tokens, ("DMGR",))
            report_path = location.path / ".glk/reports/source_qa.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["issues"], [])
            human_report = (location.path / "02_source/qa.md").read_text(encoding="utf-8")
            self.assertIn("발견된 의심 항목이 없습니다", human_report)

            cached = run_project_source_qa(
                project="qa_project", workspace_root=workspace_root
            )
            self.assertTrue(cached.cached)

            prompt_path.write_text("Air symbol: {Air}", encoding="utf-8")
            changed = run_project_source_qa(
                project="qa_project", workspace_root=workspace_root
            )
            self.assertFalse(changed.cached)
            self.assertEqual(changed.total_issues, 1)
            changed_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(changed_report["issues"][0]["code"], "TOKEN_UNKNOWN")
            human_report = (location.path / "02_source/qa.md").read_text(encoding="utf-8")
            self.assertIn("TOKEN_UNKNOWN", human_report)
            self.assertIn(blocks[0].id, human_report)

    def test_dry_run_does_not_write_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            location = create_project(name="QA Dry Run", workspace_root=workspace_root)
            write_blocks(
                location.path / ".glk/segments/source.jsonl",
                [make_block(1, "[ILLEGIBLE]", legibility="uncertain")],
            )
            result = run_project_source_qa(
                project="qa_dry_run", workspace_root=workspace_root, dry_run=True
            )
            self.assertTrue(result.dry_run)
            self.assertGreater(result.total_issues, 0)
            self.assertFalse((location.path / ".glk/reports/source_qa.json").exists())


if __name__ == "__main__":
    unittest.main()
