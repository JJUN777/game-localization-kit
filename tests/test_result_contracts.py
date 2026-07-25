from __future__ import annotations

import unittest

from glk.application.glossary_service import (
    GlossaryBuildResult,
    GlossaryImportResult,
)
from glk.application.project_service import ProjectListResult
from glk.application.segmentation_service import SegmentationResult
from glk.application.source_qa_service import SourceQaResult
from glk.application.source_registration_service import SourceRegistrationResult
from glk.application.source_review_service import (
    ReviewFinalizeResult,
    ReviewPrepareResult,
)
from glk.application.translation_prompt_service import TranslationPromptSaveResult
from glk.application.translation_review_service import (
    TranslationQaResult,
    TranslationReviewPrepareResult,
)
from glk.application.translation_service import TranslationRunResult


class ResultContractTests(unittest.TestCase):
    def test_success_only_results_do_not_publish_constant_ok(self) -> None:
        results = (
            ReviewPrepareResult(
                project_path="/workspace/demo",
                source_sha256="source",
                total_blocks=1,
                draft_file="draft",
                review_file="review",
                review_created=True,
                review_status="current",
            ),
            ReviewFinalizeResult(
                project_path="/workspace/demo",
                source_sha256="source",
                total_blocks=1,
                changed_blocks=0,
                output_file="output",
                approved_blocks_file="blocks",
                token_changes_allowed=False,
            ),
            GlossaryBuildResult(
                project_path="/workspace/demo",
                approved_source_sha256="source",
                candidate_count=1,
                output_file="glossary",
                status="current",
            ),
            GlossaryImportResult(
                project_path="/workspace/demo",
                approved_source_sha256="source",
                review_tsv_sha256="review",
                entry_count=1,
                active_count=1,
                rejected_count=0,
                manual_count=0,
                unverified_count=0,
                review_file="review",
                output_file="termbase",
            ),
            ProjectListResult(
                workspace_root="/workspace",
                projects=(),
                warnings=(),
            ),
            TranslationRunResult(
                project_path="/workspace/demo",
                model="test-model",
                approved_source_sha256="source",
                termbase_sha256="termbase",
                project_prompt_sha256="prompt",
                input_sha256="input",
                total_blocks=1,
                total_chunks=1,
                completed_blocks=1,
                completed_chunks=1,
                output_file="segments",
                draft_file="draft",
                review_file="review",
                review_status="current",
                prompt_file="prompt",
            ),
            TranslationReviewPrepareResult(
                project_path="/workspace/demo",
                total_blocks=1,
                draft_sha256="draft",
                review_file="review",
                review_created=True,
                review_status="current",
            ),
            SourceRegistrationResult(
                project_path="/workspace/demo",
                source_type="pdf",
                source_file="original.pdf",
                files=("original.pdf",),
            ),
            SegmentationResult(
                project_path="/workspace/demo",
                source_type="pdf",
                input_sha256="input",
                total_blocks=1,
                flagged_blocks=0,
                output_file="blocks",
            ),
            SourceQaResult(
                project_path="/workspace/demo",
                input_sha256="input",
                total_blocks=1,
                flagged_blocks=0,
                total_issues=0,
                error_count=0,
                warning_count=0,
                info_count=0,
                allowed_tokens=(),
                output_file="qa",
            ),
            TranslationPromptSaveResult(
                project_path="/workspace/demo",
                prompt_file="prompt",
                sha256="prompt",
                changed=True,
                translation_status_before="not_started",
                translation_invalidated=False,
                revision_file=None,
            ),
        )

        for result in results:
            with self.subTest(result=type(result).__name__):
                self.assertFalse(hasattr(result, "ok"))
                self.assertNotIn("ok", result.to_dict())

    def test_outcome_result_keeps_meaningful_ok(self) -> None:
        result = TranslationQaResult(
            project_path="/workspace/demo",
            total_blocks=1,
            error_count=1,
            warning_count=0,
            info_count=0,
            issues=(),
            json_report=None,
            markdown_report=None,
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.to_dict()["ok"])


if __name__ == "__main__":
    unittest.main()
