from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
