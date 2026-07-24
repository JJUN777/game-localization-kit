from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from glk.application.dashboard_job_service import (
    DashboardJobConflict,
    DashboardJobManager,
    run_registered_source_pipeline,
)
from glk.application.project_service import create_project
from glk.application.source_registration_service import register_project_pdf
from glk.domain.workspace import WorkspacePaths


class DashboardJobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace_root = self.root / "workspaces"
        source_pdf = self.root / "rulebook.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nsource\n")
        self.location = create_project(
            name="Background Job",
            project_id="background_job",
            workspace_root=self.workspace_root,
        )
        register_project_pdf(
            project="background_job",
            file=source_pdf,
            workspace_root=self.workspace_root,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_runs_and_persists_a_successful_source_job(self) -> None:
        completed = threading.Event()

        def runner(
            project_id: str,
            workspace_root: str | Path,
            model: str,
            progress: object,
        ) -> dict[str, object]:
            self.assertEqual(project_id, "background_job")
            self.assertEqual(Path(workspace_root), self.workspace_root.resolve())
            self.assertEqual(model, "gemini-test")
            progress("Image 1/1: source.png", 0, 1)  # type: ignore[operator]
            completed.set()
            return {"ok": True, "status": "succeeded"}

        manager = DashboardJobManager(
            self.workspace_root,
            runner=runner,
        )
        started = manager.start_source_job(
            project_id="background_job",
            model="gemini-test",
        )

        self.assertEqual(started["status"], "queued")
        self.assertTrue(completed.wait(timeout=2))
        job = manager.list_jobs()[0]
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["progress_current"], 0)
        self.assertEqual(job["progress_total"], 1)
        state_path = WorkspacePaths(
            self.location.path
        ).dashboard_source_job_state
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["job_id"], started["job_id"])
        self.assertEqual(state["status"], "succeeded")
        manager.close()

    def test_rejects_a_second_job_while_one_is_running(self) -> None:
        running = threading.Event()
        release = threading.Event()

        def runner(
            project_id: str,
            workspace_root: str | Path,
            model: str,
            progress: object,
        ) -> dict[str, object]:
            running.set()
            release.wait(timeout=2)
            return {"ok": True, "status": "succeeded"}

        manager = DashboardJobManager(
            self.workspace_root,
            runner=runner,
        )
        manager.start_source_job(
            project_id="background_job",
            model="gemini-test",
        )
        self.assertTrue(running.wait(timeout=2))

        with self.assertRaises(DashboardJobConflict):
            manager.start_source_job(
                project_id="background_job",
                model="gemini-test",
            )

        release.set()
        manager.close()

    def test_marks_a_previous_running_record_as_interrupted(self) -> None:
        state_path = WorkspacePaths(
            self.location.path
        ).dashboard_source_job_state
        state_path.write_text(
            json.dumps(
                {
                    "job_id": "old-job",
                    "project_id": "background_job",
                    "source_type": "pdf",
                    "model": "gemini-test",
                    "status": "running",
                    "progress_message": "running",
                    "progress_current": 1,
                    "progress_total": 2,
                    "result": None,
                    "error": None,
                    "created_at": "2026-07-24T00:00:00Z",
                    "started_at": "2026-07-24T00:00:01Z",
                    "finished_at": None,
                    "updated_at": "2026-07-24T00:00:01Z",
                }
            ),
            encoding="utf-8",
        )

        manager = DashboardJobManager(self.workspace_root)

        job = manager.list_jobs()[0]
        self.assertEqual(job["status"], "interrupted")
        self.assertIsNotNone(job["finished_at"])
        self.assertIn("종료", job["progress_message"])
        manager.close()

    def test_registered_pipeline_prepares_review_after_pdf_acquisition(
        self,
    ) -> None:
        planned = SimpleNamespace(selected_pages=(1, 2))
        acquisition = SimpleNamespace(
            ok=True,
            to_dict=lambda: {"ok": True, "selected_pages": [1, 2]},
        )
        segmentation = SimpleNamespace(
            to_dict=lambda: {"ok": True, "total_blocks": 3},
        )
        qa = SimpleNamespace(
            to_dict=lambda: {"ok": True, "total_issues": 0},
        )
        messages: list[str] = []
        with (
            patch(
                "glk.application.dashboard_job_service.extract_project_pdf",
                side_effect=[planned, acquisition],
            ) as extract,
            patch(
                "glk.application.dashboard_job_service.segment_project_source",
                return_value=segmentation,
            ) as segment,
            patch(
                "glk.application.dashboard_job_service.run_project_source_qa",
                return_value=qa,
            ) as source_qa,
        ):
            result = run_registered_source_pipeline(
                "background_job",
                self.workspace_root,
                "gemini-test",
                lambda message, current, total: messages.append(message),
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["source_type"], "pdf")
        self.assertEqual(extract.call_count, 2)
        self.assertTrue(extract.call_args_list[0].kwargs["dry_run"])
        self.assertEqual(
            extract.call_args_list[1].kwargs["model_name"],
            "gemini-test",
        )
        segment.assert_called_once()
        source_qa.assert_called_once()
        self.assertIn("원문 검수 준비가 완료되었습니다.", messages)


if __name__ == "__main__":
    unittest.main()
