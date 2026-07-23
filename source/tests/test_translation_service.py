from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from glk.application.project_service import create_project, inspect_project
from glk.application.translation_service import (
    TranslationError,
    build_translation_chunks,
    compile_translation_prompt,
    translate_project,
)
from glk.application.translation_review_service import (
    finalize_project_translation_review,
    prepare_project_translation_review,
    run_project_translation_qa,
)
from glk.application.translation_retry_service import retry_failed_translations
from glk.domain.approved_translation import ApprovedTranslationSegment
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock
from glk.domain.translation_segment import TranslationSegment


def make_block(order: int, text: str, *, block_type: str = "body") -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"pdf-p0001-b{order:04d}-{order:010d}",
        source_type="pdf",
        source_file="source/original.pdf",
        page=1,
        source_order=order,
        block_order=order,
        block_type=block_type,
        raw_text=text,
        corrected_text=None,
        bbox=(100.0, 100.0, 900.0, 900.0),
        legibility=None,
        status="approved",
        warnings=(),
        source_refs=(f"P001-F{order:03d}",),
        source_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def serialize_blocks(blocks: list[SourceBlock]) -> bytes:
    return "".join(
        json.dumps(block.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for block in blocks
    ).encode("utf-8")


def create_translation_project(
    workspace_root: Path, blocks: list[SourceBlock]
) -> Path:
    location = create_project(name="Translation Project", workspace_root=workspace_root)
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

    glossary_path = project_path / "terminology/glossary_review.tsv"
    glossary_path.write_text(
        "status\tsource_term\ttranslation\tcategory\tnote\tvariants\t"
        "occurrences\tlocations\texample\tcandidate_id\n",
        encoding="utf-8",
    )
    approved_hash = hashlib.sha256(approved_data).hexdigest()
    (project_path / "state/glossary_build.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "version": "glossary-candidates-local-v1",
                "approved_source_sha256": approved_hash,
                "candidate_count": 0,
            }
        ),
        encoding="utf-8",
    )
    termbase = {
        "schema_version": 1,
        "version": "termbase-import-v1",
        "project_id": "translation_project",
        "source_language": "en",
        "target_language": "ko",
        "approved_source_sha256": approved_hash,
        "review_tsv_sha256": hashlib.sha256(glossary_path.read_bytes()).hexdigest(),
        "entries": [
            {
                "candidate_id": "term-hunter",
                "source_term": "Hunter",
                "translation": "사냥꾼",
                "category": "term",
                "status": "approved",
                "note": "",
                "variants": ["Hunter", "Hunters"],
                "occurrences": 2,
                "block_ids": [blocks[1].id, blocks[2].id],
                "locations": ["p1"],
                "example": blocks[1].effective_text,
                "origin": "auto",
                "source_verified": True,
            },
            {
                "candidate_id": "term-stamina",
                "source_term": "Stamina",
                "translation": "스태미나",
                "category": "term",
                "status": "approved",
                "note": "",
                "variants": ["Stamina"],
                "occurrences": 1,
                "block_ids": [blocks[1].id],
                "locations": ["p1"],
                "example": blocks[1].effective_text,
                "origin": "auto",
                "source_verified": True,
            },
            {
                "candidate_id": "term-unused",
                "source_term": "Unused Term",
                "translation": "미사용 용어",
                "category": "term",
                "status": "approved",
                "note": "",
                "variants": ["Unused Term"],
                "occurrences": 0,
                "block_ids": [],
                "locations": [],
                "example": "",
                "origin": "manual",
                "source_verified": False,
            },
        ],
    }
    termbase_path = project_path / "terminology/termbase.json"
    termbase_path.write_text(
        json.dumps(termbase, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (project_path / "state/glossary_import.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "version": "termbase-import-v1",
                "approved_source_sha256": approved_hash,
                "review_tsv_sha256": hashlib.sha256(
                    glossary_path.read_bytes()
                ).hexdigest(),
                "termbase_sha256": hashlib.sha256(
                    termbase_path.read_bytes()
                ).hexdigest(),
                "entry_count": 3,
            }
        ),
        encoding="utf-8",
    )
    return project_path


def sample_blocks() -> list[SourceBlock]:
    return [
        make_block(1, "Combat", block_type="heading"),
        make_block(2, "Each Hunter gains 2 Stamina."),
        make_block(3, "Hunters may spend {HP} 10."),
    ]


def valid_response(blocks: list[SourceBlock]) -> dict[str, Any]:
    translations = {
        blocks[0].id: "전투",
        blocks[1].id: "각 사냥꾼은 스태미나 2를 얻습니다.",
        blocks[2].id: "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
    }
    return {
        "translations": [
            {"id": block.id, "text": translations[block.id]} for block in blocks
        ]
    }


class SequenceProvider:
    model_name = "test-model"
    prompt_version = "test-translation-v1"

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def translate(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("Unexpected translation request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class TranslationFoundationTests(unittest.TestCase):
    def test_chunks_preserve_block_boundaries_and_order(self) -> None:
        blocks = sample_blocks()
        chunks = build_translation_chunks(blocks, max_characters=20)
        self.assertEqual(
            [block.id for chunk in chunks for block in chunk.blocks],
            [block.id for block in blocks],
        )
        self.assertTrue(all(chunk.blocks for chunk in chunks))
        self.assertEqual(len(chunks), 3)

    def test_compiled_prompt_places_hard_rules_before_terms_and_project_prompt(self) -> None:
        blocks = tuple(sample_blocks()[1:2])
        entries = [
            {
                "source_term": "Hunter",
                "translation": "사냥꾼",
                "status": "approved",
                "variants": ["Hunter", "Hunters"],
                "note": "",
            },
            {
                "source_term": "Unused Term",
                "translation": "미사용 용어",
                "status": "approved",
                "variants": ["Unused Term"],
                "note": "",
            },
        ]
        prompt = compile_translation_prompt(
            blocks=blocks,
            termbase_entries=entries,
            project_instructions="Hunter를 헌터로 번역한다.",
        )
        self.assertLess(prompt.index("[NON-OVERRIDABLE"), prompt.index("사냥꾼"))
        self.assertLess(prompt.index("사냥꾼"), prompt.index("Hunter를 헌터"))
        self.assertNotIn("미사용 용어", prompt)

    def test_translates_blocks_validates_terms_and_creates_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            prompt_path = Path(temporary_directory) / "project_prompt.txt"
            prompt_path.write_text(
                "Use concise formal Korean. Hunter를 헌터로 번역한다.",
                encoding="utf-8",
            )
            provider = SequenceProvider([valid_response(blocks)])
            result = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                prompt_file=prompt_path,
                provider=provider,
            )
            self.assertEqual(result.completed_blocks, 3)
            self.assertTrue(result.review_created)
            self.assertEqual(result.review_status, "current")
            self.assertEqual(len(provider.prompts), 1)
            self.assertLess(
                provider.prompts[0].index("사냥꾼"),
                provider.prompts[0].index("Hunter를 헌터"),
            )
            segments = [
                TranslationSegment.from_dict(json.loads(line))
                for line in (
                    project_path / "segments/translation.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(segments), 3)
            self.assertEqual(
                segments[1].translated_text,
                "각 사냥꾼은 스태미나 2를 얻습니다.",
            )
            draft = (project_path / "draft/translation.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("[ORIGINAL]", draft)
            self.assertIn("[TRANSLATION]", draft)
            self.assertIn("각 사냥꾼은 스태미나 2를 얻습니다.", draft)
            pipeline = inspect_project("translation_project", workspace_root)["pipeline"]
            self.assertEqual(pipeline["translation_status"], "current")
            self.assertEqual(pipeline["translated_blocks"], 3)
            self.assertEqual(pipeline["translation_review"], "pending")

            cached_provider = SequenceProvider([])
            cached = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=cached_provider,
            )
            self.assertTrue(cached.cached)
            self.assertEqual(cached_provider.prompts, [])

    def test_retries_response_when_project_prompt_conflicts_with_termbase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            create_translation_project(workspace_root, blocks)
            invalid = valid_response(blocks)
            invalid["translations"][1]["text"] = "각 헌터는 스태미나 2를 얻습니다."
            provider = SequenceProvider([invalid, valid_response(blocks)])
            result = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=provider,
            )
            self.assertEqual(result.completed_blocks, 3)
            self.assertEqual(len(provider.prompts), 2)
            self.assertIn("VALIDATION FEEDBACK", provider.prompts[1])
            self.assertIn("must use", provider.prompts[1])

    def test_preserves_partial_state_and_resumes_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            invalid = valid_response(blocks)
            invalid["translations"][2]["text"] = "사냥꾼들은 11을 사용할 수 있습니다."
            provider = SequenceProvider([invalid, invalid])
            with self.assertRaisesRegex(TranslationError, "use --resume"):
                translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=provider,
                )
            state = json.loads(
                (project_path / "state/translation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "partial")
            self.assertEqual(state["completed_blocks"], 0)
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_status"
                ],
                "partial",
            )

            resumed = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
                resume=True,
            )
            self.assertEqual(resumed.completed_blocks, 3)

    def test_dry_run_needs_no_provider_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            result = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([]),
                dry_run=True,
                max_characters=20,
            )
            self.assertTrue(result.dry_run)
            self.assertEqual(result.total_blocks, 3)
            self.assertEqual(result.total_chunks, 3)
            self.assertFalse((project_path / "translation_prompt.txt").exists())
            self.assertFalse((project_path / "segments/translation.jsonl").exists())

    def test_prompt_edit_marks_translation_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
            )
            (project_path / "translation_prompt.txt").write_text(
                "Changed project style.", encoding="utf-8"
            )
            pipeline = inspect_project("translation_project", workspace_root)["pipeline"]
            self.assertEqual(pipeline["translation_status"], "stale")

    def test_force_retranslation_preserves_existing_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([valid_response(blocks)]),
            )
            review_path = project_path / "review/translation.txt"
            human_review = review_path.read_text(encoding="utf-8").replace(
                "전투", "전투 단계"
            )
            review_path.write_text(human_review, encoding="utf-8")

            changed = valid_response(blocks)
            changed["translations"][0]["text"] = "전투 규칙"
            result = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([changed]),
                force=True,
            )
            self.assertEqual(result.review_status, "stale")
            self.assertEqual(review_path.read_text(encoding="utf-8"), human_review)
            self.assertIn(
                "전투 규칙",
                (project_path / "draft/translation.txt").read_text(encoding="utf-8"),
            )


class TranslationReviewTests(unittest.TestCase):
    def _translated_project(
        self, workspace_root: Path
    ) -> tuple[Path, list[SourceBlock]]:
        blocks = sample_blocks()
        project_path = create_translation_project(workspace_root, blocks)
        translate_project(
            project="translation_project",
            workspace_root=workspace_root,
            provider=SequenceProvider([valid_response(blocks)]),
        )
        return project_path, blocks

    def test_qa_and_finalize_preserve_draft_and_store_only_human_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "[TRANSLATION]\n전투\n", "[TRANSLATION]\n전투 단계\n"
                ),
                encoding="utf-8",
            )

            qa = run_project_translation_qa(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertTrue(qa.passed)
            self.assertTrue(Path(qa.json_report or "").is_file())
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_review"
                ],
                "qa_passed",
            )

            dry_run = finalize_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
                dry_run=True,
            )
            self.assertTrue(dry_run.valid)
            self.assertFalse(dry_run.finalized)
            finalized = finalize_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertTrue(finalized.finalized)
            self.assertEqual(finalized.changed_blocks, 1)
            approved = [
                ApprovedTranslationSegment.from_dict(json.loads(line))
                for line in (
                    project_path / "segments/approved_translation.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(approved[0].draft_translation, "전투")
            self.assertEqual(approved[0].corrected_translation, "전투 단계")
            self.assertIsNone(approved[1].corrected_translation)
            self.assertIn(
                "전투 단계",
                (project_path / "final/translation.txt").read_text(encoding="utf-8"),
            )
            pipeline = inspect_project(
                "translation_project", workspace_root
            )["pipeline"]
            self.assertEqual(pipeline["translation_review"], "approved")
            self.assertTrue(pipeline["final_translation_approved"])

            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "전투 단계", "전투 단계 수정"
                ),
                encoding="utf-8",
            )
            stale = inspect_project(
                "translation_project", workspace_root
            )["pipeline"]
            self.assertEqual(stale["translation_review"], "stale")
            self.assertFalse(stale["final_translation_approved"])

    def test_source_or_marker_changes_are_reported_and_not_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "[ORIGINAL]\nCombat\n", "[ORIGINAL]\nChanged source\n"
                ),
                encoding="utf-8",
            )

            qa = run_project_translation_qa(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertFalse(qa.passed)
            self.assertEqual(qa.error_count, 1)
            self.assertEqual(qa.issues[0].code, "source_changed")
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_review"
                ],
                "qa_failed",
            )
            finalized = finalize_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertFalse(finalized.valid)
            self.assertFalse(
                (project_path / "segments/approved_translation.jsonl").exists()
            )

    def test_qa_blocks_number_token_and_approved_term_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            reviewed = review_path.read_text(encoding="utf-8")
            reviewed = reviewed.replace(
                "각 사냥꾼은 스태미나 2를 얻습니다.",
                "각 헌터는 스태미나 3을 얻습니다.",
            )
            reviewed = reviewed.replace(
                "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
                "헌터들은 10을 사용할 수 있습니다.",
            )
            review_path.write_text(reviewed, encoding="utf-8")

            qa = run_project_translation_qa(
                project="translation_project",
                workspace_root=workspace_root,
                dry_run=True,
            )
            codes = {issue.code for issue in qa.issues}
            self.assertIn("number_changed", codes)
            self.assertIn("curly_token_changed", codes)
            self.assertIn("approved_term_missing", codes)
            self.assertGreaterEqual(qa.error_count, 3)
            self.assertFalse((project_path / "qa/translation_qa.json").exists())

    def test_prepare_requires_force_to_reset_a_stale_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            review_path.write_text("human edits", encoding="utf-8")
            state_path = project_path / "state/translation.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["review_status"] = "stale"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            preserved = prepare_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertFalse(preserved.review_created)
            self.assertEqual(preserved.review_status, "stale")
            self.assertEqual(review_path.read_text(encoding="utf-8"), "human edits")

            reset = prepare_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
                force=True,
            )
            self.assertTrue(reset.review_created)
            self.assertEqual(reset.review_status, "current")
            self.assertEqual(
                review_path.read_bytes(),
                (project_path / "draft/translation.txt").read_bytes(),
            )
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_review"
                ],
                "pending",
            )

    def test_retry_replaces_only_qa_error_blocks_and_preserves_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, blocks = self._translated_project(workspace_root)
            draft_before = (project_path / "draft/translation.txt").read_bytes()
            review_path = project_path / "review/translation.txt"
            reviewed = review_path.read_text(encoding="utf-8")
            reviewed = reviewed.replace(
                "[TRANSLATION]\n전투\n",
                "[TRANSLATION]\n전투 단계\n",
            )
            reviewed = reviewed.replace(
                "각 사냥꾼은 스태미나 2를 얻습니다.",
                "각 사냥꾼은 스태미나 3을 얻습니다.",
            )
            review_path.write_text(reviewed, encoding="utf-8")
            qa = run_project_translation_qa(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertFalse(qa.passed)

            provider = SequenceProvider(
                [
                    {
                        "translations": [
                            {
                                "id": blocks[1].id,
                                "text": "각 사냥꾼은 스태미나 2를 얻습니다.",
                            }
                        ]
                    }
                ]
            )
            result = retry_failed_translations(
                project="translation_project",
                workspace_root=workspace_root,
                provider=provider,
            )

            self.assertEqual(result.requested_blocks, 1)
            self.assertEqual(result.retried_blocks, 1)
            self.assertEqual(result.remaining_error_count, 0)
            self.assertEqual(result.block_ids, (blocks[1].id,))
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn(blocks[1].id, provider.prompts[0])
            self.assertNotIn(blocks[0].id, provider.prompts[0])
            review_after = review_path.read_text(encoding="utf-8")
            self.assertIn("[TRANSLATION]\n전투 단계\n", review_after)
            self.assertIn("각 사냥꾼은 스태미나 2를 얻습니다.", review_after)
            self.assertEqual(
                (project_path / "draft/translation.txt").read_bytes(),
                draft_before,
            )
            revision = json.loads(
                Path(result.revision_file or "").read_text(encoding="utf-8")
            )
            self.assertEqual(revision["retried_blocks"], 1)
            self.assertEqual(
                revision["changes"][0]["previous_translation"],
                "각 사냥꾼은 스태미나 3을 얻습니다.",
            )
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_review"
                ],
                "qa_passed",
            )

    def test_retry_dry_run_lists_errors_without_calling_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, blocks = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
                    "사냥꾼들은 11을 사용할 수 있습니다.",
                ),
                encoding="utf-8",
            )
            before = review_path.read_bytes()
            provider = SequenceProvider([])
            result = retry_failed_translations(
                project="translation_project",
                workspace_root=workspace_root,
                provider=provider,
                dry_run=True,
            )
            self.assertTrue(result.dry_run)
            self.assertEqual(result.requested_blocks, 1)
            self.assertEqual(result.block_ids, (blocks[2].id,))
            self.assertEqual(provider.prompts, [])
            self.assertEqual(review_path.read_bytes(), before)
            self.assertIsNone(result.revision_file)

    def test_retry_validation_failure_does_not_partially_change_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, blocks = self._translated_project(workspace_root)
            review_path = project_path / "review/translation.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "각 사냥꾼은 스태미나 2를 얻습니다.",
                    "각 사냥꾼은 스태미나 3을 얻습니다.",
                ),
                encoding="utf-8",
            )
            before = review_path.read_bytes()
            invalid = {
                "translations": [
                    {
                        "id": blocks[1].id,
                        "text": "각 사냥꾼은 스태미나 4를 얻습니다.",
                    }
                ]
            }
            with self.assertRaisesRegex(
                TranslationError, "review was not changed"
            ):
                retry_failed_translations(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=SequenceProvider([invalid, invalid]),
                )
            self.assertEqual(review_path.read_bytes(), before)
            self.assertEqual(
                list((project_path / "revisions").glob("translation_retry_*.json")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
