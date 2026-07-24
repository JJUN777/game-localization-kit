from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from glk.application.dashboard_service import get_dashboard_document
from glk.application.project_service import create_project


class DashboardServiceTests(unittest.TestCase):
    def test_empty_workspace_returns_a_stable_empty_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"

            document = get_dashboard_document(workspace_root)

            self.assertTrue(document["ok"])
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["projects"], [])
            self.assertEqual(
                document["summary"],
                {
                    "projects": 0,
                    "in_progress": 0,
                    "completed": 0,
                    "needs_attention": 0,
                },
            )

    def test_new_project_is_present_with_reviews_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            create_project(name="Dashboard Game", workspace_root=workspace_root)

            document = get_dashboard_document(workspace_root)

            self.assertEqual(document["summary"]["projects"], 1)
            project = document["projects"][0]
            self.assertEqual(project["project_id"], "dashboard_game")
            self.assertEqual(project["stage"], "not_started")
            self.assertEqual(project["stage_label"], "시작 전")
            self.assertEqual(project["progress"], 0)
            self.assertTrue(project["workspace_ready"])
            self.assertFalse(project["reviews"]["source"]["enabled"])
            self.assertFalse(project["reviews"]["glossary"]["enabled"])
            self.assertFalse(project["reviews"]["translation"]["enabled"])


if __name__ == "__main__":
    unittest.main()
