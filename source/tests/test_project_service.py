from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from glk.application.project_service import (
    PROJECT_DIRECTORIES,
    ProjectExistsError,
    create_project,
    inspect_project,
    load_project,
)
from glk.domain.project import ProjectManifest, ProjectValidationError, normalize_project_id


class ProjectManifestTests(unittest.TestCase):
    def test_normalize_project_id_is_portable(self) -> None:
        self.assertEqual(normalize_project_id("The Elder Scrolls: Rulebook"), "the_elder_scrolls_rulebook")
        self.assertEqual(normalize_project_id("한글 룰북"), "한글_룰북")

    def test_rejects_reserved_windows_name(self) -> None:
        with self.assertRaises(ProjectValidationError):
            normalize_project_id("CON")

    def test_manifest_rejects_absolute_source_file(self) -> None:
        value = ProjectManifest.create(name="Test").to_dict()
        value["source_file"] = "/tmp/source.pdf"
        with self.assertRaises(ProjectValidationError):
            ProjectManifest.from_dict(value)


class ProjectServiceTests(unittest.TestCase):
    def test_create_and_load_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root)

            self.assertTrue((location.path / "project.json").is_file())
            for relative_path in PROJECT_DIRECTORIES:
                self.assertTrue((location.path / relative_path).is_dir())

            with (location.path / "project.json").open(encoding="utf-8") as file:
                raw_manifest = json.load(file)
            self.assertIsNone(raw_manifest["source_file"])
            self.assertEqual(load_project("demo_game", root).manifest.name, "Demo Game")
            self.assertTrue(inspect_project("demo_game", root)["ok"])

    def test_duplicate_project_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root)
            manifest_before = (location.path / "project.json").read_bytes()
            with self.assertRaises(ProjectExistsError):
                create_project(name="Demo Game", workspace_root=root)
            self.assertEqual((location.path / "project.json").read_bytes(), manifest_before)

    def test_dry_run_does_not_touch_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root, dry_run=True)
            self.assertTrue(location.dry_run)
            self.assertFalse(root.exists())

    def test_pipeline_status_distinguishes_pending_approved_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Pipeline State", workspace_root=root)
            initial = inspect_project("pipeline_state", root)["pipeline"]
            self.assertFalse(initial["source_acquired"])
            self.assertEqual(initial["human_review"], "not_ready")

            manifest = location.manifest.with_source_file("source/original.pdf")
            (location.path / "project.json").write_text(
                json.dumps(manifest.to_dict()), encoding="utf-8"
            )
            (location.path / "source/document.json").write_text(
                json.dumps({"status": "complete", "failures": []}), encoding="utf-8"
            )
            source_path = location.path / "segments/source.jsonl"
            source_path.write_text('{"block":"one"}\n', encoding="utf-8")
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            (location.path / "state/source_qa.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "source_sha256": source_hash,
                        "total_issues": 2,
                    }
                ),
                encoding="utf-8",
            )
            (location.path / "review/source.txt").write_text("review", encoding="utf-8")
            review_state_path = location.path / "state/source_review.json"
            review_state_path.write_text(
                json.dumps(
                    {
                        "status": "prepared",
                        "source_sha256": source_hash,
                    }
                ),
                encoding="utf-8",
            )

            pending = inspect_project("pipeline_state", root)["pipeline"]
            self.assertTrue(pending["source_acquired"])
            self.assertEqual(pending["qa_status"], "complete")
            self.assertEqual(pending["qa_issues"], 2)
            self.assertEqual(pending["human_review"], "pending")

            final_path = location.path / "final/source.txt"
            approved_path = location.path / "segments/approved_source.jsonl"
            final_path.write_text("final", encoding="utf-8")
            approved_path.write_text(
                "approved\n", encoding="utf-8"
            )
            review_state_path.write_text(
                json.dumps(
                    {
                        "status": "approved",
                        "source_sha256": source_hash,
                        "review_sha256": hashlib.sha256(
                            (location.path / "review/source.txt").read_bytes()
                        ).hexdigest(),
                        "final_sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
                        "approved_blocks_sha256": hashlib.sha256(
                            approved_path.read_bytes()
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            approved = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(approved["human_review"], "approved")
            self.assertTrue(approved["final_source_approved"])
            self.assertEqual(approved["glossary_status"], "not_built")

            glossary_path = location.path / "terminology/glossary_review.tsv"
            glossary_path.write_text("status\tsource_term\n", encoding="utf-8")
            (location.path / "state/glossary_build.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "version": "glossary-candidates-local-v1",
                        "approved_source_sha256": hashlib.sha256(
                            approved_path.read_bytes()
                        ).hexdigest(),
                        "candidate_count": 12,
                    }
                ),
                encoding="utf-8",
            )
            glossary_ready = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(glossary_ready["glossary_status"], "current")
            self.assertEqual(glossary_ready["glossary_candidates"], 12)
            self.assertEqual(glossary_ready["termbase_status"], "not_built")

            termbase_path = location.path / "terminology/termbase.json"
            termbase_path.write_text('{"entries": []}\n', encoding="utf-8")
            (location.path / "state/glossary_import.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "version": "termbase-import-v1",
                        "approved_source_sha256": hashlib.sha256(
                            approved_path.read_bytes()
                        ).hexdigest(),
                        "review_tsv_sha256": hashlib.sha256(
                            glossary_path.read_bytes()
                        ).hexdigest(),
                        "termbase_sha256": hashlib.sha256(
                            termbase_path.read_bytes()
                        ).hexdigest(),
                        "entry_count": 9,
                    }
                ),
                encoding="utf-8",
            )
            termbase_ready = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(termbase_ready["termbase_status"], "current")
            self.assertEqual(termbase_ready["termbase_entries"], 9)
            self.assertEqual(termbase_ready["translation_status"], "not_run")

            glossary_path.write_text(
                "status\tsource_term\nrejected\tchanged\n", encoding="utf-8"
            )
            termbase_stale = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(termbase_stale["termbase_status"], "stale")
            self.assertEqual(termbase_stale["translation_status"], "not_ready")

            (location.path / "review/source.txt").write_text(
                "edited after approval", encoding="utf-8"
            )
            modified = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(modified["human_review"], "pending")
            self.assertFalse(modified["final_source_approved"])

            source_path.write_text('{"block":"changed"}\n', encoding="utf-8")
            stale = inspect_project("pipeline_state", root)["pipeline"]
            self.assertEqual(stale["qa_status"], "stale")
            self.assertEqual(stale["human_review"], "stale")
            self.assertFalse(stale["final_source_approved"])
            self.assertEqual(stale["glossary_status"], "stale")
            self.assertEqual(stale["termbase_status"], "stale")


if __name__ == "__main__":
    unittest.main()
