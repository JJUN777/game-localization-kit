from __future__ import annotations

import json
import hashlib
from importlib.resources import files
import tempfile
import unittest
from pathlib import Path

from glk.application.project_service import (
    DEFAULT_OCR_PROMPT,
    GLOSSARY_BUILD_VERSION,
    PROJECT_DIRECTORIES,
    PROJECT_INPUT_DIRECTORIES,
    SOURCE_QA_VERSION,
    ProjectExistsError,
    create_project,
    inspect_project,
    list_projects,
    load_project,
    load_workspace_project_id,
)
from glk.domain.project import ProjectManifest, ProjectValidationError, normalize_project_id


class ProjectManifestTests(unittest.TestCase):
    def test_normalize_project_id_is_portable(self) -> None:
        self.assertEqual(normalize_project_id("The Elder Scrolls: Rulebook"), "the_elder_scrolls_rulebook")
        with self.assertRaises(ProjectValidationError):
            normalize_project_id("한글 룰북")

    def test_project_id_accepts_only_portable_ascii_characters(self) -> None:
        manifest = ProjectManifest.create(name="한글 룰북", project_id="korean_rulebook_2")
        self.assertEqual(manifest.project_id, "korean_rulebook_2")
        for project_id in ("한글_룰북", "game-name", "Game_Name", "_game", "game__name"):
            with self.subTest(project_id=project_id):
                with self.assertRaises(ProjectValidationError):
                    ProjectManifest.create(name="Test", project_id=project_id)

    def test_rejects_reserved_windows_name(self) -> None:
        with self.assertRaises(ProjectValidationError):
            normalize_project_id("CON")

    def test_manifest_rejects_absolute_source_file(self) -> None:
        value = ProjectManifest.create(name="Test").to_dict()
        value["source_file"] = "/tmp/source.pdf"
        with self.assertRaises(ProjectValidationError):
            ProjectManifest.from_dict(value)

    def test_manifest_rejects_legacy_workspace_schema(self) -> None:
        for legacy_version in (1, 2):
            with self.subTest(schema_version=legacy_version):
                value = ProjectManifest.create(name="Test").to_dict()
                value["schema_version"] = legacy_version
                with self.assertRaisesRegex(
                    ProjectValidationError,
                    f"Unsupported project schema version: {legacy_version}",
                ):
                    ProjectManifest.from_dict(value)


class ProjectServiceTests(unittest.TestCase):
    def test_create_and_load_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root)

            self.assertTrue((location.path / "project.json").is_file())
            prompt_path = location.path / DEFAULT_OCR_PROMPT
            self.assertTrue(prompt_path.is_file())
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("# 프로젝트 공통 OCR 추가 지침", prompt)
            self.assertIn("TOKEN_NAME", prompt)
            self.assertIn("[ICON: concise visible description]", prompt)
            self.assertIn("가상 예시이며 실제 OCR 규칙이 아닙니다", prompt)
            self.assertNotRegex(prompt, r"\{[A-Za-z][A-Za-z0-9_]*\}")
            for relative_path in PROJECT_INPUT_DIRECTORIES + PROJECT_DIRECTORIES:
                self.assertTrue((location.path / relative_path).is_dir())

            with (location.path / "project.json").open(encoding="utf-8") as file:
                raw_manifest = json.load(file)
            self.assertEqual(raw_manifest["schema_version"], 3)
            self.assertIsNone(raw_manifest["source_file"])
            self.assertEqual(load_project("demo_game", root).manifest.name, "Demo Game")
            self.assertTrue(inspect_project("demo_game", root)["ok"])
            self.assertEqual(
                {
                    path.name
                    for path in location.path.iterdir()
                    if path.is_dir()
                },
                {
                    "01_input",
                    "02_source",
                    "03_terminology",
                    "04_translation",
                    "05_output",
                    ".glk",
                },
            )
            for legacy_directory in (
                "input",
                "source",
                "segments",
                "draft",
                "review",
                "final",
                "terminology",
                "qa",
                "state",
                "output",
            ):
                self.assertFalse((location.path / legacy_directory).exists())
            self.assertFalse((location.path / "02_source/assets").exists())

    def test_elder_scrolls_poc_prompt_is_preserved_as_an_example(self) -> None:
        prompt = (
            files("glk.templates")
            .joinpath("elder_scrolls_ocr_prompt.example.txt")
            .read_text(encoding="utf-8")
        )
        self.assertIn("# Elder Scrolls POC OCR prompt 예제", prompt)
        self.assertIn("{DMGR}", prompt)
        self.assertIn("{eWater}", prompt)
        self.assertIn("{tWater}", prompt)

    def test_duplicate_project_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root)
            manifest_before = (location.path / "project.json").read_bytes()
            with self.assertRaises(ProjectExistsError):
                create_project(name="Demo Game", workspace_root=root)
            self.assertEqual((location.path / "project.json").read_bytes(), manifest_before)

    def test_load_workspace_project_id_rejects_paths_and_noncanonical_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Safe Project", workspace_root=root)

            loaded = load_workspace_project_id("safe_project", root)
            self.assertEqual(loaded.path, location.path)

            for project_id in (
                "../safe_project",
                "safe_project/child",
                "Safe_Project",
                "safe__project",
            ):
                with self.subTest(project_id=project_id):
                    with self.assertRaises(ProjectValidationError):
                        load_workspace_project_id(project_id, root)

            manifest_path = location.path / "project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["project_id"] = "different_project"
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with self.assertRaises(ProjectValidationError):
                load_workspace_project_id("safe_project", root)

    def test_dry_run_does_not_touch_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Demo Game", workspace_root=root, dry_run=True)
            self.assertTrue(location.dry_run)
            self.assertIn(DEFAULT_OCR_PROMPT, location.created_paths)
            self.assertFalse(root.exists())

    def test_lists_projects_and_skips_damaged_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            beta = create_project(name="Beta Game", workspace_root=root)
            alpha = create_project(name="Alpha Game", workspace_root=root)
            (alpha.path / "01_input/pdf/rulebook.pdf").write_bytes(b"%PDF-1.4\n")
            (beta.path / "01_input/images/card.png").write_bytes(b"image")
            damaged = root / "damaged"
            damaged.mkdir()
            (damaged / "project.json").write_text("{", encoding="utf-8")
            (root / "unrelated").mkdir()

            result = list_projects(root)

            self.assertEqual(
                [project.project_id for project in result.projects],
                ["alpha_game", "beta_game"],
            )
            self.assertEqual(result.projects[0].source_type, "pdf")
            self.assertEqual(result.projects[1].source_type, "images")
            self.assertEqual(result.projects[0].stage, "not_started")
            self.assertFalse(result.projects[0].final_translation_approved)
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual(result.warnings[0].directory, "damaged")

    def test_pipeline_status_distinguishes_pending_approved_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspaces"
            location = create_project(name="Pipeline State", workspace_root=root)
            initial = inspect_project("pipeline_state", root)["pipeline"]
            self.assertFalse(initial["source_acquired"])
            self.assertEqual(initial["human_review"], "not_ready")

            manifest = location.manifest.with_source_file(
                "01_input/pdf/rulebook.pdf"
            )
            (location.path / "project.json").write_text(
                json.dumps(manifest.to_dict()), encoding="utf-8"
            )
            (location.path / ".glk/state/pdf_acquisition.json").write_text(
                json.dumps({"status": "complete", "failures": []}), encoding="utf-8"
            )
            source_path = location.path / ".glk/segments/source.jsonl"
            source_path.write_text('{"block":"one"}\n', encoding="utf-8")
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            (location.path / ".glk/state/source_qa.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "version": SOURCE_QA_VERSION,
                        "source_sha256": source_hash,
                        "total_issues": 2,
                    }
                ),
                encoding="utf-8",
            )
            (location.path / "02_source/review.txt").write_text("review", encoding="utf-8")
            review_state_path = location.path / ".glk/state/source_review.json"
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

            final_path = location.path / "02_source/final.txt"
            approved_path = location.path / ".glk/segments/approved_source.jsonl"
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
                            (location.path / "02_source/review.txt").read_bytes()
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

            glossary_path = location.path / "03_terminology/glossary_review.tsv"
            glossary_path.write_text("status\tsource_term\n", encoding="utf-8")
            (location.path / ".glk/state/glossary_build.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "version": GLOSSARY_BUILD_VERSION,
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

            termbase_path = location.path / "03_terminology/termbase.json"
            termbase_path.write_text('{"entries": []}\n', encoding="utf-8")
            (location.path / ".glk/state/glossary_import.json").write_text(
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

            (location.path / "02_source/review.txt").write_text(
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
