from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from glk.application.glossary_service import (
    GlossaryBuildError,
    GlossaryImportError,
    GLOSSARY_REVIEW_COLUMNS,
    build_project_glossary_candidates,
    extract_glossary_candidates,
    import_project_glossary,
)
from glk.application.project_service import create_project, inspect_project
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock


def make_block(
    order: int,
    text: str,
    *,
    block_type: str = "body",
    status: str = "approved",
) -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"pdf-p0001-b{order:04d}-{order:010d}",
        source_type="pdf",
        source_file="source/original.pdf",
        page=1 + (order // 3),
        source_order=order,
        block_order=order,
        block_type=block_type,
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility=None,
        status=status,
        warnings=(),
        source_refs=(f"P001-F{order:03d}",),
        source_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def serialize_blocks(blocks: list[SourceBlock]) -> bytes:
    return "".join(
        json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for block in blocks
    ).encode("utf-8")


def create_approved_project(workspace_root: Path, blocks: list[SourceBlock]) -> Path:
    location = create_project(name="Glossary Project", workspace_root=workspace_root)
    project_path = location.path
    raw_blocks = [
        SourceBlock.from_dict({**block.to_dict(), "status": "raw"}) for block in blocks
    ]
    source_data = serialize_blocks(raw_blocks)
    approved_data = serialize_blocks(blocks)
    source_path = project_path / "segments/source.jsonl"
    approved_path = project_path / "segments/approved_source.jsonl"
    review_path = project_path / "review/source.txt"
    final_path = project_path / "final/source.txt"
    source_path.write_bytes(source_data)
    approved_path.write_bytes(approved_data)
    review_path.write_text("review source", encoding="utf-8")
    final_path.write_text("final source", encoding="utf-8")
    (project_path / "state/source_review.json").write_text(
        json.dumps(
            {
                "status": "approved",
                "source_sha256": hashlib.sha256(source_data).hexdigest(),
                "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
                "final_sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
                "approved_blocks_sha256": hashlib.sha256(approved_data).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return project_path


def sample_blocks() -> list[SourceBlock]:
    return [
        make_block(1, "Furwing", block_type="title"),
        make_block(2, "Each Hunter gains 2 Stamina."),
        make_block(3, "Hunters may spend Stamina to perform an Action."),
        make_block(4, "PRIMAL ATTACK", block_type="heading"),
        make_block(5, "Resolve the Primal Attack. The Hunter loses Stamina and gains {HP}."),
    ]


def read_review_rows(path: Path) -> list[dict[str, str]]:
    return list(
        csv.DictReader(
            io.StringIO(path.read_text(encoding="utf-8-sig")),
            delimiter="\t",
        )
    )


def write_review_rows(path: Path, rows: list[dict[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=GLOSSARY_REVIEW_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(buffer.getvalue().encode("utf-8-sig"))


def reject_all(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row["status"] = "rejected"


class GlossaryCandidateRuleTests(unittest.TestCase):
    def test_extracts_repeated_terms_headings_variants_and_locations(self) -> None:
        candidates = extract_glossary_candidates(sample_blocks(), min_frequency=2)
        by_key = {candidate.source_term.casefold(): candidate for candidate in candidates}
        self.assertIn("furwing", by_key)
        self.assertIn("hunter", by_key)
        self.assertIn("stamina", by_key)
        self.assertIn("primal attack", by_key)
        self.assertNotIn("hp", by_key)
        self.assertIn("Hunters", by_key["hunter"].variants)
        self.assertGreaterEqual(by_key["hunter"].occurrences, 3)
        self.assertTrue(by_key["stamina"].locations)
        self.assertEqual(by_key["furwing"].category, "proper_noun")


class GlossaryBuildServiceTests(unittest.TestCase):
    def test_builds_editable_tsv_and_preserves_human_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())

            first = build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            self.assertTrue(first.created)
            self.assertGreater(first.candidate_count, 0)
            output_path = project_path / "terminology/glossary_review.tsv"
            rows = list(
                csv.DictReader(
                    io.StringIO(output_path.read_text(encoding="utf-8-sig")),
                    delimiter="\t",
                )
            )
            self.assertEqual(len(rows), first.candidate_count)
            self.assertTrue(all(row["status"] == "review" for row in rows))
            self.assertTrue(all(row["candidate_id"].startswith("term-") for row in rows))

            edited = output_path.read_text(encoding="utf-8-sig").replace(
                "review\tHunter\t", "approved\tHunter\t헌터\t"
            )
            output_path.write_text(edited, encoding="utf-8-sig")
            edited_bytes = output_path.read_bytes()
            cached = build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            self.assertTrue(cached.cached)
            self.assertEqual(output_path.read_bytes(), edited_bytes)

            stale = build_project_glossary_candidates(
                project="glossary_project",
                workspace_root=workspace_root,
                min_frequency=3,
            )
            self.assertEqual(stale.status, "stale")
            self.assertEqual(output_path.read_bytes(), edited_bytes)

            reset = build_project_glossary_candidates(
                project="glossary_project",
                workspace_root=workspace_root,
                min_frequency=3,
                force=True,
            )
            self.assertTrue(reset.reset)
            self.assertNotIn("헌터", output_path.read_text(encoding="utf-8-sig"))

    def test_rejects_project_without_current_approved_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            create_project(name="Pending Glossary", workspace_root=workspace_root)
            with self.assertRaises(GlossaryBuildError):
                build_project_glossary_candidates(
                    project="pending_glossary", workspace_root=workspace_root
                )

    def test_dry_run_does_not_write_tsv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())
            result = build_project_glossary_candidates(
                project="glossary_project",
                workspace_root=workspace_root,
                dry_run=True,
            )
            self.assertTrue(result.dry_run)
            self.assertGreater(result.candidate_count, 0)
            self.assertFalse((project_path / "terminology/glossary_review.tsv").exists())


class GlossaryImportServiceTests(unittest.TestCase):
    def test_imports_empty_review_when_source_has_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(
                workspace_root, [make_block(1, "The game.")]
            )
            build = build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            self.assertEqual(build.candidate_count, 0)
            result = import_project_glossary(
                project="glossary_project",
                file="terminology/glossary_review.tsv",
                workspace_root=workspace_root,
            )
            self.assertEqual(result.entry_count, 0)
            termbase = json.loads(
                (project_path / "terminology/termbase.json").read_text(encoding="utf-8")
            )
            self.assertEqual(termbase["entries"], [])

    def test_imports_reviewed_tsv_and_enriches_manual_term_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = [
                *sample_blocks(),
                make_block(6, "A critical hit deals extra damage."),
            ]
            project_path = create_approved_project(workspace_root, blocks)
            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            review_path = project_path / "terminology/glossary_review.tsv"
            rows = read_review_rows(review_path)
            reject_all(rows)
            hunter = next(row for row in rows if row["source_term"] == "Hunter")
            hunter["status"] = "approved"
            hunter["translation"] = "사냥꾼"
            stamina = next(row for row in rows if row["source_term"] == "Stamina")
            stamina["status"] = "keep"
            rows.append(
                {
                    "status": "approved",
                    "source_term": "critical hit",
                    "translation": "치명타",
                    "category": "term",
                    "note": "항상 치명타로 번역",
                    "variants": "",
                    "occurrences": "",
                    "locations": "",
                    "example": "",
                    "candidate_id": "",
                }
            )
            write_review_rows(review_path, rows)

            result = import_project_glossary(
                project="glossary_project",
                file="terminology/glossary_review.tsv",
                workspace_root=workspace_root,
            )
            self.assertEqual(result.entry_count, len(rows))
            self.assertEqual(result.active_count, 3)
            self.assertEqual(result.manual_count, 1)
            self.assertEqual(result.unverified_count, 0)

            termbase = json.loads(
                (project_path / "terminology/termbase.json").read_text(encoding="utf-8")
            )
            by_source = {entry["source_term"]: entry for entry in termbase["entries"]}
            self.assertEqual(by_source["Hunter"]["translation"], "사냥꾼")
            self.assertEqual(by_source["Stamina"]["translation"], "Stamina")
            manual = by_source["critical hit"]
            self.assertTrue(manual["candidate_id"].startswith("manual-"))
            self.assertEqual(manual["origin"], "manual")
            self.assertTrue(manual["source_verified"])
            self.assertEqual(manual["occurrences"], 1)
            self.assertTrue(manual["block_ids"])

            enriched = next(
                row
                for row in read_review_rows(review_path)
                if row["source_term"] == "critical hit"
            )
            self.assertTrue(enriched["candidate_id"].startswith("manual-"))
            self.assertEqual(enriched["occurrences"], "1")
            self.assertTrue(enriched["locations"])
            self.assertIn("critical hit", enriched["example"])

            cached = import_project_glossary(
                project="glossary_project",
                file=review_path,
                workspace_root=workspace_root,
            )
            self.assertTrue(cached.cached)
            pipeline = inspect_project("glossary_project", workspace_root)["pipeline"]
            self.assertEqual(pipeline["termbase_status"], "current")
            self.assertEqual(pipeline["termbase_entries"], len(rows))

    def test_allows_explicit_unverified_manual_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())
            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            review_path = project_path / "terminology/glossary_review.tsv"
            rows = read_review_rows(review_path)
            reject_all(rows)
            rows.append(
                {
                    "status": "approved",
                    "source_term": "Future Keyword",
                    "translation": "미래 키워드",
                    "category": "term",
                    "note": "확장판 선등록",
                    "variants": "",
                    "occurrences": "",
                    "locations": "",
                    "example": "",
                    "candidate_id": "",
                }
            )
            write_review_rows(review_path, rows)

            with self.assertRaisesRegex(GlossaryImportError, "was not found"):
                import_project_glossary(
                    project="glossary_project",
                    file=review_path,
                    workspace_root=workspace_root,
                )
            self.assertFalse((project_path / "terminology/termbase.json").exists())

            result = import_project_glossary(
                project="glossary_project",
                file=review_path,
                workspace_root=workspace_root,
                allow_missing_terms=True,
            )
            self.assertEqual(result.unverified_count, 1)
            self.assertTrue(result.warnings)
            termbase = json.loads(
                (project_path / "terminology/termbase.json").read_text(encoding="utf-8")
            )
            future = next(
                entry
                for entry in termbase["entries"]
                if entry["source_term"] == "Future Keyword"
            )
            self.assertFalse(future["source_verified"])
            self.assertEqual(future["occurrences"], 0)

    def test_rejects_unfinished_review_and_preserves_existing_termbase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())
            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            review_path = project_path / "terminology/glossary_review.tsv"
            rows = read_review_rows(review_path)
            reject_all(rows)
            write_review_rows(review_path, rows)
            import_project_glossary(
                project="glossary_project",
                file=review_path,
                workspace_root=workspace_root,
            )
            termbase_path = project_path / "terminology/termbase.json"
            existing_termbase = termbase_path.read_bytes()

            rows = read_review_rows(review_path)
            rows[0]["status"] = "review"
            write_review_rows(review_path, rows)
            with self.assertRaisesRegex(GlossaryImportError, "still in review"):
                import_project_glossary(
                    project="glossary_project",
                    file=review_path,
                    workspace_root=workspace_root,
                )
            self.assertEqual(termbase_path.read_bytes(), existing_termbase)

    def test_rejects_deleted_or_changed_generated_candidate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())
            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            review_path = project_path / "terminology/glossary_review.tsv"
            rows = read_review_rows(review_path)
            reject_all(rows)
            rows.pop()
            write_review_rows(review_path, rows)
            with self.assertRaisesRegex(GlossaryImportError, "deleted generated"):
                import_project_glossary(
                    project="glossary_project",
                    file=review_path,
                    workspace_root=workspace_root,
                )

            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root, force=True
            )
            rows = read_review_rows(review_path)
            reject_all(rows)
            rows[0]["candidate_id"] = "term-000000000000"
            write_review_rows(review_path, rows)
            with self.assertRaisesRegex(GlossaryImportError, "unknown or changed"):
                import_project_glossary(
                    project="glossary_project",
                    file=review_path,
                    workspace_root=workspace_root,
                )

    def test_rejects_invalid_editable_fields_and_protected_tokens(self) -> None:
        cases = (
            ("approved", "", "term", "Hunter", "translation is empty"),
            ("rejected", "", "unknown", "Hunter", "invalid category"),
            ("approved", "체력", "term", "{HP}", "protected token"),
        )
        for status, translation, category, source_term, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary_directory:
                workspace_root = Path(temporary_directory) / "workspaces"
                project_path = create_approved_project(workspace_root, sample_blocks())
                build_project_glossary_candidates(
                    project="glossary_project", workspace_root=workspace_root
                )
                review_path = project_path / "terminology/glossary_review.tsv"
                rows = read_review_rows(review_path)
                reject_all(rows)
                rows[0]["status"] = status
                rows[0]["translation"] = translation
                rows[0]["category"] = category
                rows[0]["source_term"] = source_term
                if source_term == "{HP}":
                    rows[0]["candidate_id"] = ""
                write_review_rows(review_path, rows)
                with self.assertRaisesRegex(GlossaryImportError, message):
                    import_project_glossary(
                        project="glossary_project",
                        file=review_path,
                        workspace_root=workspace_root,
                    )

    def test_rejects_manual_case_or_plural_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path = create_approved_project(workspace_root, sample_blocks())
            build_project_glossary_candidates(
                project="glossary_project", workspace_root=workspace_root
            )
            review_path = project_path / "terminology/glossary_review.tsv"
            rows = read_review_rows(review_path)
            reject_all(rows)
            rows.append(
                {
                    "status": "rejected",
                    "source_term": "Hunters",
                    "translation": "",
                    "category": "term",
                    "note": "",
                    "variants": "",
                    "occurrences": "",
                    "locations": "",
                    "example": "",
                    "candidate_id": "",
                }
            )
            write_review_rows(review_path, rows)
            with self.assertRaisesRegex(GlossaryImportError, "case/plural variant"):
                import_project_glossary(
                    project="glossary_project",
                    file=review_path,
                    workspace_root=workspace_root,
                )


if __name__ == "__main__":
    unittest.main()
