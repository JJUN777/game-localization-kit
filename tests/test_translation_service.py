from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from glk.application import translation_service
from glk.application._io import append_bytes_durable
from glk.application.glossary_service import GLOSSARY_BUILD_VERSION
from glk.application.project_service import create_project, inspect_project
from glk.application.translation_service import (
    TranslationError,
    build_translation_chunks,
    compile_translation_prompt,
    translate_project,
    validate_translation_response,
)
from glk.application.translation_review_service import (
    _final_translation_outputs,
    _render_final_translation,
    finalize_project_translation_review,
    get_project_translation_review_document,
    prepare_project_translation_review,
    run_project_translation_qa,
)
from glk.application.translation_retry_service import retry_failed_translations
from glk.domain.approved_translation import ApprovedTranslationSegment
from glk.domain.source_block import SOURCE_BLOCK_SCHEMA_VERSION, SourceBlock
from glk.domain.translation_qa import check_translation_contract
from glk.domain.translation_segment import TranslationSegment
from glk.domain.workspace import IMAGE_SOURCE_ROOT, WorkspacePaths


def make_block(order: int, text: str, *, block_type: str = "body") -> SourceBlock:
    return SourceBlock(
        schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
        id=f"pdf-p0001-b{order:04d}-{order:010d}",
        source_type="pdf",
        source_file="01_input/pdf/rulebook.pdf",
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
    source_path = project_path / ".glk/segments/source.jsonl"
    approved_path = project_path / ".glk/segments/approved_source.jsonl"
    review_path = project_path / "02_source/review.txt"
    final_path = project_path / "02_source/final.txt"
    source_path.write_bytes(source_data)
    approved_path.write_bytes(approved_data)
    review_path.write_text("review source", encoding="utf-8")
    final_path.write_text("final source", encoding="utf-8")
    (project_path / ".glk/state/source_review.json").write_text(
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

    glossary_path = project_path / "03_terminology/glossary_review.tsv"
    glossary_path.write_text(
        "status\tsource_term\ttranslation\tcategory\tnote\tvariants\t"
        "occurrences\tlocations\texample\tcandidate_id\n",
        encoding="utf-8",
    )
    approved_hash = hashlib.sha256(approved_data).hexdigest()
    (project_path / ".glk/state/glossary_build.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "version": GLOSSARY_BUILD_VERSION,
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
    termbase_path = project_path / "03_terminology/termbase.json"
    termbase_path.write_text(
        json.dumps(termbase, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (project_path / ".glk/state/glossary_import.json").write_text(
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
    def test_writes_translation_segments_once_instead_of_rewriting_prefixes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = [
                make_block(index, f"Rule {index}: resolve this effect.")
                for index in range(1, 13)
            ]
            project_path = create_translation_project(workspace_root, blocks)
            responses = [
                {
                    "translations": [
                        {
                            "id": block.id,
                            "text": f"규칙 {index}: 이 효과를 해결합니다.",
                        }
                    ]
                }
                for index, block in enumerate(blocks, start=1)
            ]
            output_writes: list[int] = []
            original_atomic = translation_service._write_bytes_atomic
            original_append = translation_service._append_bytes_durable

            def measured_atomic(path: Path, value: bytes) -> None:
                if path.name == "translation.jsonl":
                    output_writes.append(len(value))
                original_atomic(path, value)

            def measured_append(path: Path, value: bytes) -> None:
                if path.name == "translation.jsonl":
                    output_writes.append(len(value))
                original_append(path, value)

            with (
                patch(
                    "glk.application.translation_service._write_bytes_atomic",
                    side_effect=measured_atomic,
                ),
                patch(
                    "glk.application.translation_service._append_bytes_durable",
                    side_effect=measured_append,
                ),
            ):
                result = translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=SequenceProvider(responses),
                    max_characters=1,
                )

            output_path = (
                project_path / ".glk/segments/translation.jsonl"
            )
            self.assertEqual(result.total_chunks, len(blocks))
            self.assertEqual(sum(output_writes), output_path.stat().st_size)
            self.assertEqual(len(output_writes), len(blocks))

    def test_resume_discards_uncheckpointed_append_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            translations = {
                blocks[0].id: "전투",
                blocks[1].id: "각 사냥꾼은 스태미나 2를 얻습니다.",
                blocks[2].id: "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
            }

            def response_for(block: SourceBlock) -> dict[str, Any]:
                return {
                    "translations": [
                        {"id": block.id, "text": translations[block.id]}
                    ]
                }

            invalid = {"translations": []}
            with self.assertRaisesRegex(TranslationError, "use --resume"):
                translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=SequenceProvider(
                        [response_for(blocks[0]), invalid, invalid]
                    ),
                    max_characters=20,
                )
            output_path = (
                project_path / ".glk/segments/translation.jsonl"
            )
            state_path = project_path / ".glk/state/translation.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            checkpoint_bytes = state["translation_output_bytes"]
            self.assertEqual(checkpoint_bytes, output_path.stat().st_size)

            append_bytes_durable(output_path, b'{"interrupted":')
            self.assertGreater(output_path.stat().st_size, checkpoint_bytes)

            resumed = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider(
                    [response_for(blocks[1]), response_for(blocks[2])]
                ),
                max_characters=20,
                resume=True,
            )

            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.completed_blocks, len(blocks))
            self.assertNotIn(b"interrupted", output_path.read_bytes())
            completed_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                completed_state["translation_output_bytes"],
                output_path.stat().st_size,
            )

    def test_resume_finishes_artifacts_after_interrupted_final_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            draft_path = project_path / "04_translation/draft.txt"
            original_atomic = translation_service._write_bytes_atomic
            interrupted = False

            def interrupt_draft(path: Path, value: bytes) -> None:
                nonlocal interrupted
                if path == draft_path and not interrupted:
                    interrupted = True
                    raise OSError("simulated interruption")
                original_atomic(path, value)

            with (
                patch(
                    "glk.application.translation_service._write_bytes_atomic",
                    side_effect=interrupt_draft,
                ),
                self.assertRaisesRegex(OSError, "simulated interruption"),
            ):
                translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=SequenceProvider([valid_response(blocks)]),
                )

            state_path = project_path / ".glk/state/translation.json"
            partial_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(partial_state["status"], "partial")
            self.assertEqual(
                partial_state["completed_blocks"],
                len(blocks),
            )
            self.assertFalse(draft_path.exists())

            resumed = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([]),
                resume=True,
            )

            self.assertTrue(resumed.resumed)
            self.assertTrue(draft_path.is_file())
            completed_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(completed_state["status"], "complete")

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

    def test_rejected_terms_are_not_prompted_or_validated_as_keep(self) -> None:
        blocks = (make_block(1, "Each player draws five cards."),)
        entries = [
            {
                "source_term": "player",
                "translation": "player",
                "status": "keep",
                "variants": ["player", "players"],
                "note": "keep-marker",
            },
            {
                "source_term": "cards",
                "translation": "",
                "status": "rejected",
                "variants": ["card", "cards"],
                "note": "rejected-marker",
            },
        ]

        prompt = compile_translation_prompt(
            blocks=blocks,
            termbase_entries=entries,
            project_instructions="Translate naturally.",
        )

        self.assertIn("keep-marker", prompt)
        self.assertNotIn("rejected-marker", prompt)
        self.assertEqual(
            check_translation_contract(
                source_text=blocks[0].effective_text,
                translated_text="Each player는 카드 다섯 장을 뽑습니다.",
                termbase_entries=entries,
            ),
            [],
        )
        self.assertEqual(
            [
                issue.code
                for issue in check_translation_contract(
                    source_text=blocks[0].effective_text,
                    translated_text="각 플레이어는 카드 다섯 장을 뽑습니다.",
                    termbase_entries=entries,
                )
            ],
            ["keep_term_changed"],
        )

    def test_keep_terms_use_placeholders_and_are_restored_before_validation(
        self,
    ) -> None:
        block = make_block(
            1,
            "Each player shuffles the deck. All players draw.",
        )
        entries = [
            {
                "source_term": "player",
                "translation": "player",
                "status": "keep",
                "variants": ["player", "players"],
                "note": "",
            },
            {
                "source_term": "deck",
                "translation": "deck",
                "status": "keep",
                "variants": ["deck"],
                "note": "",
            },
        ]

        prompt = compile_translation_prompt(
            blocks=(block,),
            termbase_entries=entries,
            project_instructions="Translate naturally.",
        )

        self.assertIn(
            "Each {GLK_KEEP_0001} shuffles the {GLK_KEEP_0002}. "
            "All {GLK_KEEP_0003} draw.",
            prompt,
        )
        self.assertIn("Preserve every {GLK_KEEP_####} placeholder", prompt)
        translated = validate_translation_response(
            response={
                "translations": [
                    {
                        "id": block.id,
                        "text": (
                            "각 {GLK_KEEP_0001}는 {GLK_KEEP_0002}을 섞습니다. "
                            "모든 {GLK_KEEP_0003}가 뽑습니다."
                        ),
                    }
                ]
            },
            blocks=(block,),
            termbase_entries=entries,
        )
        self.assertEqual(
            translated[block.id],
            "각 player는 deck을 섞습니다. 모든 players가 뽑습니다.",
        )

    def test_number_validation_allows_words_and_korean_singular_counters(
        self,
    ) -> None:
        self.assertEqual(
            check_translation_contract(
                source_text="Each player draws five cards.",
                translated_text="각 플레이어는 카드 5장을 뽑습니다.",
                termbase_entries=[],
            ),
            [],
        )
        self.assertEqual(
            check_translation_contract(
                source_text="Reveal the top event card.",
                translated_text="맨 위 이벤트 카드 1장을 공개합니다.",
                termbase_entries=[],
            ),
            [],
        )
        issues = check_translation_contract(
            source_text="Combat",
            translated_text="전투 123123",
            termbase_entries=[],
        )
        self.assertEqual([issue.code for issue in issues], ["number_changed"])

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
                    project_path / ".glk/segments/translation.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(segments), 3)
            self.assertEqual(
                segments[1].translated_text,
                "각 사냥꾼은 스태미나 2를 얻습니다.",
            )
            draft = (project_path / "04_translation/draft.txt").read_text(
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
            self.assertIn("확정 용어", provider.prompts[1])

    def test_preserves_content_issues_for_human_review_after_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            invalid = valid_response(blocks)
            invalid["translations"][2]["text"] = (
                "사냥꾼들은 11을 사용할 수 있습니다."
            )
            provider = SequenceProvider([invalid, invalid])

            result = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=provider,
            )

            self.assertEqual(result.completed_blocks, 3)
            self.assertEqual(result.validation_issue_blocks, 1)
            self.assertGreaterEqual(result.validation_issue_count, 2)
            self.assertEqual(len(provider.prompts), 2)
            self.assertIn("VALIDATION FEEDBACK", provider.prompts[1])
            state = json.loads(
                (project_path / ".glk/state/translation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["validation_issue_blocks"], 1)
            segment_path = (
                project_path / ".glk/segments/translation.jsonl"
            )
            segments = [
                TranslationSegment.from_dict(json.loads(line))
                for line in segment_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(
                {
                    segment.source_block_id: segment.status
                    for segment in segments
                },
                {
                    blocks[0].id: "translated",
                    blocks[1].id: "translated",
                    blocks[2].id: "flagged",
                },
            )
            segment_data = segment_path.read_bytes()
            cached = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider([]),
            )
            self.assertTrue(cached.cached)
            self.assertEqual(cached.validation_issue_blocks, 1)
            self.assertEqual(segment_path.read_bytes(), segment_data)
            self.assertTrue(
                (project_path / "04_translation/review.txt").is_file()
            )
            qa = run_project_translation_qa(
                project="translation_project",
                workspace_root=workspace_root,
                dry_run=True,
            )
            self.assertFalse(qa.passed)
            self.assertGreaterEqual(qa.error_count, 2)

            review_path = project_path / "04_translation/review.txt"
            review_path.write_text(
                review_path.read_text(encoding="utf-8").replace(
                    "사냥꾼들은 11을 사용할 수 있습니다.",
                    "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
                ),
                encoding="utf-8",
            )
            finalized = finalize_project_translation_review(
                project="translation_project",
                workspace_root=workspace_root,
            )
            self.assertTrue(finalized.finalized)
            approved_path = (
                project_path
                / ".glk/segments/approved_translation.jsonl"
            )
            approved = [
                ApprovedTranslationSegment.from_dict(json.loads(line))
                for line in approved_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertTrue(
                all(segment.status == "approved" for segment in approved)
            )
            corrected = next(
                segment
                for segment in approved
                if segment.source_block_id == blocks[2].id
            )
            self.assertEqual(
                corrected.corrected_translation,
                "사냥꾼들은 {HP} 10을 사용할 수 있습니다.",
            )

    def test_resume_preserves_flagged_segments_and_issue_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(
                workspace_root,
                blocks,
            )
            valid_by_id = {
                item["id"]: item["text"]
                for item in valid_response(blocks)["translations"]
            }
            first_valid = {
                "translations": [
                    {
                        "id": blocks[0].id,
                        "text": valid_by_id[blocks[0].id],
                    }
                ]
            }
            second_invalid = {
                "translations": [
                    {
                        "id": blocks[1].id,
                        "text": "각 사냥꾼은 스태미나 3을 얻습니다.",
                    }
                ]
            }
            missing_third = {"translations": []}

            with self.assertRaisesRegex(TranslationError, "use --resume"):
                translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=SequenceProvider(
                        [
                            first_valid,
                            second_invalid,
                            second_invalid,
                            missing_third,
                            missing_third,
                        ]
                    ),
                    max_characters=1,
                )

            segment_path = (
                project_path / ".glk/segments/translation.jsonl"
            )
            partial_segments = [
                TranslationSegment.from_dict(json.loads(line))
                for line in segment_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(len(partial_segments), 2)
            self.assertEqual(
                [segment.status for segment in partial_segments],
                ["translated", "flagged"],
            )
            partial_state = json.loads(
                (
                    project_path / ".glk/state/translation.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                partial_state["validation_issue_blocks"],
                1,
            )

            resumed = translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider(
                    [
                        {
                            "translations": [
                                {
                                    "id": blocks[2].id,
                                    "text": valid_by_id[blocks[2].id],
                                }
                            ]
                        }
                    ]
                ),
                max_characters=1,
                resume=True,
            )

            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.validation_issue_blocks, 1)
            completed_segments = [
                TranslationSegment.from_dict(json.loads(line))
                for line in segment_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [segment.status for segment in completed_segments],
                ["translated", "flagged", "translated"],
            )

    def test_preserves_partial_state_and_resumes_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            blocks = sample_blocks()
            project_path = create_translation_project(workspace_root, blocks)
            invalid = {"translations": []}
            provider = SequenceProvider([invalid, invalid])
            with self.assertRaisesRegex(TranslationError, "use --resume"):
                translate_project(
                    project="translation_project",
                    workspace_root=workspace_root,
                    provider=provider,
                )
            state = json.loads(
                (project_path / ".glk/state/translation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "partial")
            self.assertEqual(state["completed_blocks"], 0)
            self.assertEqual(
                state["hard_rules_version"],
                "translation-hard-rules-v3",
            )
            self.assertIn("missing ids", state["failure_reason"])
            self.assertEqual(
                inspect_project("translation_project", workspace_root)["pipeline"][
                    "translation_status"
                ],
                "partial",
            )
            state["input_sha256"] = "previous-hard-rules-input"
            (project_path / ".glk/state/translation.json").write_text(
                json.dumps(state),
                encoding="utf-8",
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
            self.assertFalse((project_path / "04_translation/prompt.txt").exists())
            self.assertFalse((project_path / ".glk/segments/translation.jsonl").exists())

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
            (project_path / "04_translation/prompt.txt").write_text(
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
            review_path = project_path / "04_translation/review.txt"
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
                (project_path / "04_translation/draft.txt").read_text(encoding="utf-8"),
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

    def test_review_document_includes_relevant_and_full_active_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            _, blocks = self._translated_project(workspace_root)

            document = get_project_translation_review_document(
                project="translation_project",
                workspace_root=workspace_root,
            )

            self.assertEqual(len(document["termbase"]), 3)
            by_id = {block["id"]: block for block in document["blocks"]}
            self.assertEqual(by_id[blocks[0].id]["relevant_terms"], [])
            self.assertEqual(
                [
                    term["source_term"]
                    for term in by_id[blocks[1].id]["relevant_terms"]
                ],
                ["Hunter", "Stamina"],
            )
            self.assertEqual(
                [
                    term["source_term"]
                    for term in by_id[blocks[2].id]["relevant_terms"]
                ],
                ["Hunter"],
            )

    def test_keep_only_block_is_info_instead_of_untranslated_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            block = make_block(1, "IMPORTANT", block_type="heading")
            other_blocks = [
                make_block(2, "Setup", block_type="heading"),
                make_block(3, "End", block_type="heading"),
            ]
            project_path = create_translation_project(
                workspace_root,
                [block, *other_blocks],
            )
            paths = WorkspacePaths(project_path)
            termbase = json.loads(paths.termbase.read_text(encoding="utf-8"))
            termbase["entries"] = [
                {
                    "candidate_id": "term-important",
                    "source_term": "IMPORTANT",
                    "translation": "IMPORTANT",
                    "category": "ui",
                    "status": "keep",
                    "note": "",
                    "variants": ["IMPORTANT"],
                    "occurrences": 1,
                    "block_ids": [block.id],
                    "locations": ["p1"],
                    "example": "IMPORTANT",
                    "origin": "auto",
                    "source_verified": True,
                }
            ]
            termbase_data = (
                json.dumps(termbase, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            paths.termbase.write_bytes(termbase_data)
            import_state = json.loads(
                paths.glossary_import_state.read_text(encoding="utf-8")
            )
            import_state["termbase_sha256"] = hashlib.sha256(
                termbase_data
            ).hexdigest()
            import_state["entry_count"] = 1
            paths.glossary_import_state.write_text(
                json.dumps(import_state),
                encoding="utf-8",
            )
            translate_project(
                project="translation_project",
                workspace_root=workspace_root,
                provider=SequenceProvider(
                    [
                        {
                            "translations": [
                                {"id": block.id, "text": "IMPORTANT"},
                                {"id": other_blocks[0].id, "text": "준비"},
                                {"id": other_blocks[1].id, "text": "종료"},
                            ]
                        }
                    ]
                ),
            )

            document = get_project_translation_review_document(
                project="translation_project",
                workspace_root=workspace_root,
            )

            issues = document["blocks"][0]["issues"]
            self.assertEqual(
                [issue["code"] for issue in issues],
                ["keep_rule_applied"],
            )
            self.assertEqual(issues[0]["severity"], "info")
            self.assertEqual(
                document["blocks"][0]["relevant_terms"][0]["status"],
                "keep",
            )
            self.assertEqual(document["summary"]["warnings"], 0)
            self.assertEqual(document["summary"]["info"], 1)

    def test_qa_and_finalize_preserve_draft_and_store_only_human_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "04_translation/review.txt"
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
                    project_path / ".glk/segments/approved_translation.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(approved[0].draft_translation, "전투")
            self.assertEqual(approved[0].corrected_translation, "전투 단계")
            self.assertIsNone(approved[1].corrected_translation)
            final_text = (
                project_path / "05_output/rulebook_kor.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                final_text,
                "[PAGE 1]\n\n"
                "전투 단계\n\n"
                "각 사냥꾼은 스태미나 2를 얻습니다.\n\n"
                "사냥꾼들은 {HP} 10을 사용할 수 있습니다.\n",
            )
            self.assertNotIn("[BLOCK", final_text)
            self.assertNotIn("[[GLK_", final_text)
            page_split = _render_final_translation(
                [approved[0], approved[1], replace(approved[2], page=2)]
            ).decode("utf-8")
            self.assertIn(
                "\n\n----------------------\n\n"
                "[PAGE 2]\n\n사냥꾼들은 {HP} 10을 사용할 수 있습니다.\n",
                page_split,
            )
            self.assertFalse(
                (project_path / "05_output/translation.txt").exists()
            )
            image_outputs = _final_translation_outputs(
                WorkspacePaths(project_path),
                IMAGE_SOURCE_ROOT,
                [
                    replace(
                        approved[0],
                        source_file="01_input/images/cards/card-01.png",
                        page=None,
                    ),
                    replace(
                        approved[1],
                        source_file="01_input/images/cards/card-01.png",
                        page=None,
                    ),
                    replace(
                        approved[2],
                        source_file="01_input/images/characters/hero.jpg",
                        page=None,
                    ),
                ],
            )
            self.assertEqual(
                {
                    path.relative_to(project_path).as_posix()
                    for path in image_outputs
                },
                {
                    "05_output/combined_kor.txt",
                    "05_output/cards/card-01_kor.txt",
                    "05_output/characters/hero_kor.txt",
                },
            )
            self.assertEqual(
                image_outputs[
                    project_path / "05_output/cards/card-01_kor.txt"
                ].decode("utf-8"),
                "전투 단계\n\n각 사냥꾼은 스태미나 2를 얻습니다.\n",
            )
            self.assertEqual(
                image_outputs[
                    project_path / "05_output/combined_kor.txt"
                ].decode("utf-8"),
                "[card-01.png]\n\n"
                "전투 단계\n\n"
                "각 사냥꾼은 스태미나 2를 얻습니다.\n\n"
                "----------------------\n\n"
                "[hero.jpg]\n\n"
                "사냥꾼들은 {HP} 10을 사용할 수 있습니다.\n",
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
            review_path = project_path / "04_translation/review.txt"
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
                (project_path / ".glk/segments/approved_translation.jsonl").exists()
            )

    def test_qa_blocks_changed_or_unexpected_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "04_translation/review.txt"
            reviewed = review_path.read_text(encoding="utf-8")
            reviewed = reviewed.replace(
                "[TRANSLATION]\n전투\n",
                "[TRANSLATION]\n전투 123123\n",
            )
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
            number_messages = [
                issue.message
                for issue in qa.issues
                if issue.code == "number_changed"
            ]
            self.assertTrue(
                any(
                    "원문: 없음 / 번역: 123123" in message
                    for message in number_messages
                )
            )
            self.assertTrue(
                any(
                    "원문: 2 / 번역: 3" in message
                    for message in number_messages
                )
            )
            term_issue = next(
                issue
                for issue in qa.issues
                if issue.code == "approved_term_missing"
            )
            self.assertIn("확정 용어", term_issue.message)
            self.assertGreaterEqual(qa.error_count, 3)
            self.assertFalse((project_path / ".glk/reports/translation_qa.json").exists())

    def test_prepare_requires_force_to_reset_a_stale_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_root = Path(temporary_directory) / "workspaces"
            project_path, _ = self._translated_project(workspace_root)
            review_path = project_path / "04_translation/review.txt"
            review_path.write_text("human edits", encoding="utf-8")
            state_path = project_path / ".glk/state/translation.json"
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
                (project_path / "04_translation/draft.txt").read_bytes(),
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
            draft_before = (project_path / "04_translation/draft.txt").read_bytes()
            review_path = project_path / "04_translation/review.txt"
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
                (project_path / "04_translation/draft.txt").read_bytes(),
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
            review_path = project_path / "04_translation/review.txt"
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
            review_path = project_path / "04_translation/review.txt"
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
