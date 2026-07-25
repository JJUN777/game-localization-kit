from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from glk.application.dashboard_job_service import (
    DashboardJobConflict,
    DashboardJobError,
    DashboardJobManager,
    _safe_translation_error,
    run_glossary_pipeline,
    run_registered_source_pipeline,
    run_translation_pipeline,
)
from glk.application.project_service import create_project
from glk.application.source_registration_service import register_project_pdf
from glk.domain.workspace import WorkspacePaths
from tests.test_glossary_service import create_approved_project, sample_blocks
from tests.test_translation_service import (
    create_translation_project,
    make_block as make_translation_block,
)


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

    def test_translation_validation_error_keeps_actionable_cause(self) -> None:
        error = ValueError(
            "Translation failed for chunk-test. Cause: "
            "block-1: 원문 유지 용어 'player'가 번역문에서 변경되었습니다."
        )

        message = _safe_translation_error(error, "gemini-test")

        self.assertIn("검증 규칙을 통과하지 못했습니다", message)
        self.assertIn("원문 유지 용어 'player'", message)

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

    def test_upgrades_a_saved_generic_partial_record(self) -> None:
        state_path = WorkspacePaths(
            self.location.path
        ).dashboard_source_job_state
        state_path.write_text(
            json.dumps(
                {
                    "job_id": "old-partial-job",
                    "project_id": "background_job",
                    "source_type": "pdf",
                    "model": "missing-model",
                    "status": "partial",
                    "progress_message": "일부 원본 처리에 실패했습니다.",
                    "progress_current": 1,
                    "progress_total": 1,
                    "result": {
                        "ok": False,
                        "status": "partial",
                        "source_type": "pdf",
                        "error": None,
                        "acquisition": {
                            "selected_pages": [1],
                            "successful_pages": [],
                            "failures": [
                                {
                                    "page": 1,
                                    "error": (
                                        "404 NOT_FOUND: missing-model is not "
                                        "found for API version v1beta"
                                    ),
                                }
                            ],
                        },
                        "segmentation": None,
                        "qa": None,
                    },
                    "error": (
                        "일부 원본 처리에 실패했습니다. "
                        "결과를 확인한 뒤 다시 시도하세요."
                    ),
                    "created_at": "2026-07-24T00:00:00Z",
                    "started_at": "2026-07-24T00:00:01Z",
                    "finished_at": "2026-07-24T00:00:02Z",
                    "updated_at": "2026-07-24T00:00:02Z",
                }
            ),
            encoding="utf-8",
        )

        manager = DashboardJobManager(self.workspace_root)

        job = manager.list_jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("'missing-model'", job["error"])
        self.assertIn("모델을 확인", job["error"])
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["result"]["status"], "failed")
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

    def test_registered_pipeline_marks_all_provider_failures_as_failed(
        self,
    ) -> None:
        planned = SimpleNamespace(selected_pages=(1, 2))
        acquisition_data = {
            "ok": False,
            "selected_pages": [1, 2],
            "successful_pages": [],
            "failures": (
                {
                    "page": 1,
                    "error": (
                        "404 NOT_FOUND: models/missing-model is not found "
                        "for API version v1beta"
                    ),
                },
                {
                    "page": 2,
                    "error": (
                        "404 NOT_FOUND: model is not supported for "
                        "generateContent"
                    ),
                },
            ),
        }
        acquisition = SimpleNamespace(
            ok=False,
            to_dict=lambda: acquisition_data,
        )
        with (
            patch(
                "glk.application.dashboard_job_service.extract_project_pdf",
                side_effect=[planned, acquisition],
            ),
            patch(
                "glk.application.dashboard_job_service.segment_project_source"
            ) as segment,
            patch(
                "glk.application.dashboard_job_service.run_project_source_qa"
            ) as source_qa,
        ):
            result = run_registered_source_pipeline(
                "background_job",
                self.workspace_root,
                "missing-model",
                lambda message, current, total: None,
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("'missing-model'", result["error"])
        self.assertIn("모델을 확인", result["error"])
        self.assertNotIn("v1beta", result["error"])
        segment.assert_not_called()
        source_qa.assert_not_called()

    def test_registered_pipeline_keeps_mixed_results_partial(
        self,
    ) -> None:
        planned = SimpleNamespace(selected_pages=(1, 2))
        acquisition_data = {
            "ok": False,
            "selected_pages": [1, 2],
            "successful_pages": [1],
            "failures": (
                {
                    "page": 2,
                    "error": "429 RESOURCE_EXHAUSTED: quota exceeded",
                },
            ),
        }
        acquisition = SimpleNamespace(
            ok=False,
            to_dict=lambda: acquisition_data,
        )
        with patch(
            "glk.application.dashboard_job_service.extract_project_pdf",
            side_effect=[planned, acquisition],
        ):
            result = run_registered_source_pipeline(
                "background_job",
                self.workspace_root,
                "gemini-test",
                lambda message, current, total: None,
            )

        self.assertEqual(result["status"], "partial")
        self.assertIn("전체 2개 중 1개", result["error"])
        self.assertIn("사용량", result["error"])

    def test_manager_preserves_safe_error_for_failed_runner(self) -> None:
        completed = threading.Event()

        def runner(
            project_id: str,
            workspace_root: str | Path,
            model: str,
            progress: object,
        ) -> dict[str, object]:
            completed.set()
            return {
                "ok": False,
                "status": "failed",
                "error": "선택한 Gemini 모델을 사용할 수 없습니다.",
            }

        manager = DashboardJobManager(
            self.workspace_root,
            runner=runner,
        )
        manager.start_source_job(
            project_id="background_job",
            model="missing-model",
        )
        self.assertTrue(completed.wait(timeout=2))
        state_path = WorkspacePaths(
            self.location.path
        ).dashboard_source_job_state
        for _ in range(100):
            job = manager.list_jobs()[0]
            if job["status"] == "failed":
                break
            threading.Event().wait(0.01)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(
            state["error"],
            "선택한 Gemini 모델을 사용할 수 없습니다.",
        )
        self.assertEqual(
            state["progress_message"],
            "원문 준비 작업에 실패했습니다.",
        )
        manager.close()

    def test_runs_and_persists_a_successful_glossary_job(self) -> None:
        create_approved_project(self.workspace_root, sample_blocks())
        completed = threading.Event()

        def glossary_runner(
            project_id: str,
            workspace_root: str | Path,
            progress: object,
        ) -> dict[str, object]:
            self.assertEqual(project_id, "glossary_project")
            self.assertEqual(Path(workspace_root), self.workspace_root.resolve())
            progress("용어 후보를 생성하고 있습니다.", 1, 2)  # type: ignore[operator]
            completed.set()
            return {
                "ok": True,
                "status": "succeeded",
                "glossary": {"candidate_count": 4},
            }

        manager = DashboardJobManager(
            self.workspace_root,
            glossary_runner=glossary_runner,
        )
        started = manager.start_glossary_job(project_id="glossary_project")

        self.assertEqual(started["status"], "queued")
        self.assertTrue(completed.wait(timeout=2))
        state_path = WorkspacePaths(
            self.workspace_root / "glossary_project"
        ).dashboard_glossary_job_state
        for _ in range(100):
            job = manager.list_glossary_jobs()[0]
            if job["status"] == "succeeded":
                break
            threading.Event().wait(0.01)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["result"]["glossary"]["candidate_count"], 4)
        self.assertEqual(
            manager.list_glossary_jobs()[0]["project_id"],
            "glossary_project",
        )
        manager.close()

    def test_glossary_pipeline_builds_candidates_without_a_model(self) -> None:
        project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        messages: list[str] = []

        result = run_glossary_pipeline(
            "glossary_project",
            self.workspace_root,
            lambda message, current, total: messages.append(message),
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertGreater(
            result["glossary"]["candidate_count"],  # type: ignore[index]
            0,
        )
        self.assertTrue(
            (project_path / "03_terminology/glossary_review.tsv").is_file()
        )
        self.assertIn("용어 후보 생성이 완료되었습니다.", messages)

    def test_rejects_glossary_job_before_source_approval(self) -> None:
        manager = DashboardJobManager(self.workspace_root)

        with self.assertRaises(DashboardJobError):
            manager.start_glossary_job(project_id="background_job")

        manager.close()

    def test_glossary_job_blocks_other_background_jobs(self) -> None:
        create_approved_project(self.workspace_root, sample_blocks())
        running = threading.Event()
        release = threading.Event()

        def glossary_runner(
            project_id: str,
            workspace_root: str | Path,
            progress: object,
        ) -> dict[str, object]:
            running.set()
            release.wait(timeout=2)
            return {"ok": True, "status": "succeeded"}

        manager = DashboardJobManager(
            self.workspace_root,
            glossary_runner=glossary_runner,
        )
        manager.start_glossary_job(project_id="glossary_project")
        self.assertTrue(running.wait(timeout=2))

        with self.assertRaises(DashboardJobConflict):
            manager.start_source_job(
                project_id="background_job",
                model="gemini-test",
            )

        release.set()
        manager.close()

    def test_marks_a_previous_glossary_job_as_interrupted(self) -> None:
        project_path = create_approved_project(
            self.workspace_root,
            sample_blocks(),
        )
        state_path = WorkspacePaths(
            project_path
        ).dashboard_glossary_job_state
        state_path.write_text(
            json.dumps(
                {
                    "job_id": "old-glossary-job",
                    "project_id": "glossary_project",
                    "status": "running",
                    "progress_message": "running",
                    "progress_current": 1,
                    "progress_total": 2,
                    "result": None,
                    "error": None,
                    "created_at": "2026-07-25T00:00:00Z",
                    "started_at": "2026-07-25T00:00:01Z",
                    "finished_at": None,
                    "updated_at": "2026-07-25T00:00:01Z",
                }
            ),
            encoding="utf-8",
        )

        manager = DashboardJobManager(self.workspace_root)

        job = manager.list_glossary_jobs()[0]
        self.assertEqual(job["status"], "interrupted")
        self.assertIsNotNone(job["finished_at"])
        self.assertIn("종료", job["progress_message"])
        manager.close()

    def test_runs_and_persists_a_successful_translation_job(self) -> None:
        project_path = create_translation_project(
            self.workspace_root,
            [
                make_translation_block(1, "COMBAT", block_type="heading"),
                make_translation_block(2, "Each Hunter gains 2 Stamina."),
                make_translation_block(3, "Hunters may spend Stamina."),
            ],
        )
        completed = threading.Event()

        def translation_runner(
            project_id: str,
            workspace_root: str | Path,
            model: str,
            resume: bool,
            force: bool,
            progress: object,
        ) -> dict[str, object]:
            self.assertEqual(project_id, "translation_project")
            self.assertEqual(Path(workspace_root), self.workspace_root.resolve())
            self.assertEqual(model, "gemini-test")
            self.assertFalse(resume)
            self.assertFalse(force)
            progress("Chunk 1/1: requesting translation", 0, 1)  # type: ignore[operator]
            completed.set()
            return {
                "ok": True,
                "status": "succeeded",
                "translation": {"completed_blocks": 3},
            }

        manager = DashboardJobManager(
            self.workspace_root,
            translation_runner=translation_runner,
        )
        started = manager.start_translation_job(
            project_id="translation_project",
            model="gemini-test",
            prompt="Translate naturally.",
        )

        self.assertEqual(started["status"], "queued")
        self.assertFalse(started["resume"])
        self.assertTrue(completed.wait(timeout=2))
        state_path = WorkspacePaths(
            project_path
        ).dashboard_translation_job_state
        for _ in range(100):
            job = manager.list_translation_jobs()[0]
            if job["status"] == "succeeded":
                break
            threading.Event().wait(0.01)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(
            state["result"]["translation"]["completed_blocks"],
            3,
        )
        self.assertEqual(
            manager.list_translation_jobs()[0]["project_id"],
            "translation_project",
        )
        self.assertEqual(
            WorkspacePaths(project_path).translation_prompt.read_text(
                encoding="utf-8"
            ),
            "Translate naturally.",
        )
        manager.close()

    def test_partial_translation_requires_its_saved_prompt(self) -> None:
        project_path = create_translation_project(
            self.workspace_root,
            [
                make_translation_block(1, "COMBAT", block_type="heading"),
                make_translation_block(2, "Each Hunter gains 2 Stamina."),
                make_translation_block(3, "Hunters may spend Stamina."),
            ],
        )
        paths = WorkspacePaths(project_path)
        saved_prompt = "Keep the saved style."
        paths.translation_prompt.write_text(saved_prompt, encoding="utf-8")
        paths.translation_state.write_text(
            json.dumps(
                {
                    "version": "translation-run-v1",
                    "status": "partial",
                    "approved_source_sha256": hashlib.sha256(
                        paths.approved_source_segments.read_bytes()
                    ).hexdigest(),
                    "termbase_sha256": hashlib.sha256(
                        paths.termbase.read_bytes()
                    ).hexdigest(),
                    "project_prompt_sha256": hashlib.sha256(
                        saved_prompt.encode("utf-8")
                    ).hexdigest(),
                    "completed_blocks": 0,
                }
            ),
            encoding="utf-8",
        )
        manager = DashboardJobManager(
            self.workspace_root,
            translation_runner=lambda *args: {
                "ok": True,
                "status": "succeeded",
            },
        )

        with self.assertRaisesRegex(DashboardJobError, "saved prompt"):
            manager.start_translation_job(
                project_id="translation_project",
                model="gemini-test",
                prompt="Changed style.",
            )

        started = manager.start_translation_job(
            project_id="translation_project",
            model="gemini-test",
            prompt=saved_prompt,
        )
        self.assertTrue(started["resume"])
        manager.close()

    def test_translation_pipeline_reports_chunk_progress(self) -> None:
        planned = SimpleNamespace(total_chunks=2)
        translated = SimpleNamespace(
            validation_issue_count=0,
            to_dict=lambda: {
                "completed_blocks": 3,
                "completed_chunks": 2,
            }
        )
        progress: list[tuple[str, int | None, int | None]] = []
        with patch(
            "glk.application.dashboard_job_service.translate_project",
            side_effect=[planned, translated],
        ) as translate:
            result = run_translation_pipeline(
                "translation_project",
                self.workspace_root,
                "gemini-test",
                False,
                False,
                lambda message, current, total: progress.append(
                    (message, current, total)
                ),
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(result["qa"])
        self.assertEqual(translate.call_count, 2)
        self.assertTrue(translate.call_args_list[0].kwargs["dry_run"])
        self.assertFalse(translate.call_args_list[1].kwargs["resume"])
        self.assertFalse(translate.call_args_list[1].kwargs["force"])
        translate.call_args_list[1].kwargs["progress"](
            "Chunk 2/2: requesting translation"
        )
        self.assertIn(
            ("Chunk 2/2: requesting translation", 1, 2),
            progress,
        )

    def test_translation_pipeline_runs_qa_for_reviewable_content_issues(
        self,
    ) -> None:
        planned = SimpleNamespace(total_chunks=1)
        translated = SimpleNamespace(
            validation_issue_count=2,
            to_dict=lambda: {
                "completed_blocks": 3,
                "completed_chunks": 1,
                "validation_issue_count": 2,
                "validation_issue_blocks": 1,
            },
        )
        qa_result = SimpleNamespace(
            error_count=2,
            to_dict=lambda: {
                "error_count": 2,
                "warning_count": 0,
                "passed": False,
            },
        )
        progress: list[tuple[str, int | None, int | None]] = []
        with (
            patch(
                "glk.application.dashboard_job_service.translate_project",
                side_effect=[planned, translated],
            ),
            patch(
                "glk.application.dashboard_job_service.run_project_translation_qa",
                return_value=qa_result,
            ) as qa,
        ):
            result = run_translation_pipeline(
                "translation_project",
                self.workspace_root,
                "gemini-test",
                False,
                False,
                lambda message, current, total: progress.append(
                    (message, current, total)
                ),
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["qa"]["error_count"], 2)
        qa.assert_called_once_with(
            project="translation_project",
            workspace_root=self.workspace_root,
        )
        self.assertIn(
            (
                "초벌 번역이 완료되었습니다. "
                "번역 검수에서 2개 오류를 확인하세요.",
                1,
                1,
            ),
            progress,
        )


if __name__ == "__main__":
    unittest.main()
