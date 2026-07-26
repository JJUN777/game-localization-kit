from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from glk.application.project_service import inspect_project, load_project
from glk.application.translation_prompt_service import (
    TranslationPromptError,
    load_translation_prompt_document,
    save_project_translation_prompt,
)
from glk.application.translation_restart_service import (
    archive_translation_restart,
    clear_stale_translation_review_artifacts,
)
from glk.application.translation_review_service import (
    finalize_project_translation_review,
    prepare_project_translation_review,
)
from glk.application.translation_service import translate_project
from glk.domain.workspace import WorkspacePaths
from tests.test_translation_service import (
    SequenceProvider,
    create_translation_project,
    sample_blocks,
    valid_response,
)


class TranslationPromptServiceTests(unittest.TestCase):
    def test_newline_only_prompt_change_keeps_translation_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translate_project(
                project=project_path,
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
            )
            paths = WorkspacePaths(project_path)
            prompt = paths.translation_prompt.read_text(encoding="utf-8")
            self.assertIn("\n", prompt)
            paths.translation_prompt.write_bytes(
                prompt.replace("\n", "\r\n").encode("utf-8")
            )

            pipeline = inspect_project(project_path)["pipeline"]
            cached = translate_project(
                project=project_path,
                workspace_root=workspace_root,
                provider=SequenceProvider([]),
            )

            self.assertEqual(pipeline["translation_status"], "current")
            self.assertTrue(cached.cached)

    def test_saves_default_prompt_without_running_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            project_path = create_translation_project(
                workspace_root,
                sample_blocks(),
            )
            paths = WorkspacePaths(project_path)
            paths.translation_prompt.unlink(missing_ok=True)
            location = load_project(project_path, workspace_root)

            document = load_translation_prompt_document(project_path)
            self.assertFalse(document.saved)
            result = save_project_translation_prompt(
                location,
                document.value,
                expected_sha256=document.sha256,
            )

            self.assertFalse(result.changed)
            self.assertFalse(result.translation_invalidated)
            self.assertTrue(paths.translation_prompt.is_file())
            self.assertEqual(
                paths.translation_prompt.read_text(encoding="utf-8"),
                document.value,
            )
            self.assertFalse(paths.translation_state.is_file())

    def test_changed_prompt_marks_translation_stale_and_keeps_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translate_project(
                project=project_path,
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
            )
            location = load_project(project_path, workspace_root)
            document = load_translation_prompt_document(project_path)

            result = save_project_translation_prompt(
                location,
                "Use short imperative sentences.",
                expected_sha256=document.sha256,
            )

            self.assertTrue(result.changed)
            self.assertTrue(result.translation_invalidated)
            self.assertIsNotNone(result.revision_file)
            revision = json.loads(
                (project_path / str(result.revision_file)).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                revision["previous_prompt_sha256"],
                document.sha256,
            )
            self.assertEqual(
                inspect_project(project_path)["pipeline"]["translation_status"],
                "stale",
            )
            with self.assertRaisesRegex(
                TranslationPromptError,
                "새로고침",
            ):
                save_project_translation_prompt(
                    location,
                    "Another style.",
                    expected_sha256=document.sha256,
                )

    def test_full_restart_archives_and_resets_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translate_project(
                project=project_path,
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
            )
            finalize_project_translation_review(
                project=project_path,
                workspace_root=workspace_root,
            )
            location = load_project(project_path, workspace_root)
            paths = WorkspacePaths(project_path)

            revision_path = archive_translation_restart(location)
            self.assertIsNotNone(revision_path)
            assert revision_path is not None
            self.assertTrue(
                (revision_path / "05_output/rulebook_kor.txt").is_file()
            )
            self.assertTrue(
                (revision_path / "04_translation/review.txt").is_file()
            )

            changed = valid_response(blocks)
            changed["translations"][0]["text"] = "전투 규칙"
            translate_project(
                project=project_path,
                workspace_root=workspace_root,
                provider=SequenceProvider([changed]),
                force=True,
            )
            clear_stale_translation_review_artifacts(location)
            prepare_project_translation_review(
                project=project_path,
                workspace_root=workspace_root,
                force=True,
            )

            self.assertFalse(
                (project_path / "05_output/rulebook_kor.txt").exists()
            )
            self.assertFalse(paths.translation_review_state.exists())
            self.assertEqual(
                paths.translation_review.read_bytes(),
                paths.translation_draft.read_bytes(),
            )
            pipeline = inspect_project(project_path)["pipeline"]
            self.assertEqual(pipeline["translation_status"], "current")
            self.assertEqual(pipeline["translation_review"], "pending")


if __name__ == "__main__":
    unittest.main()
