from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest

from glk.domain.approved_translation import (
    APPROVED_TRANSLATION_SCHEMA_VERSION,
    ApprovedTranslationSegment,
    ApprovedTranslationValidationError,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _segment(
    corrected_translation: str | None = "수정된 번역입니다.",
) -> ApprovedTranslationSegment:
    source_text = "Original source."
    draft_translation = "초벌 번역입니다."
    effective_translation = corrected_translation or draft_translation
    return ApprovedTranslationSegment(
        schema_version=APPROVED_TRANSLATION_SCHEMA_VERSION,
        source_block_id="pdf-p0001-b0001",
        source_file="01_input/pdf/rulebook.pdf",
        page=1,
        source_order=1,
        block_type="paragraph",
        source_text=source_text,
        source_sha256=_digest(source_text),
        draft_translation=draft_translation,
        draft_translation_sha256=_digest(draft_translation),
        corrected_translation=corrected_translation,
        final_translation_sha256=_digest(effective_translation),
        status="approved",
        model="test-model",
        prompt_sha256="a" * 64,
        termbase_sha256="b" * 64,
    )


class ApprovedTranslationSegmentTests(unittest.TestCase):
    def test_round_trips_corrected_and_unchanged_segments(self) -> None:
        for segment in (_segment(), _segment(corrected_translation=None)):
            with self.subTest(corrected=segment.corrected_translation is not None):
                payload = segment.to_dict()
                self.assertEqual(
                    ApprovedTranslationSegment.from_dict(payload),
                    segment,
                )
                self.assertEqual(
                    segment.effective_translation,
                    segment.corrected_translation or segment.draft_translation,
                )

    def test_unchanged_correction_must_be_null(self) -> None:
        segment = _segment()
        unchanged = replace(
            segment,
            corrected_translation=segment.draft_translation,
            final_translation_sha256=segment.draft_translation_sha256,
        )

        with self.assertRaisesRegex(
            ApprovedTranslationValidationError,
            "Unchanged text must use null corrected_translation",
        ):
            unchanged.validate()

    def test_rejects_invalid_schema_identity_and_status_fields(self) -> None:
        cases = (
            (
                replace(_segment(), schema_version=999),
                "Unsupported approved translation schema",
            ),
            (
                replace(_segment(), source_block_id="INVALID ID"),
                "Invalid source block ID",
            ),
            (replace(_segment(), page=True), "page must be a positive integer"),
            (
                replace(_segment(), source_order=0),
                "source_order must be a positive integer",
            ),
            (
                replace(_segment(), corrected_translation=" "),
                "corrected_translation must be null or non-empty",
            ),
            (
                replace(_segment(), status="translated"),
                "Invalid approved translation status",
            ),
        )
        for segment, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    ApprovedTranslationValidationError,
                    message,
                ):
                    segment.validate()

    def test_rejects_invalid_or_mismatched_hashes(self) -> None:
        cases = (
            (
                replace(_segment(), termbase_sha256="not-a-digest"),
                "termbase_sha256 must be a SHA-256 hex digest",
            ),
            (
                replace(_segment(), draft_translation_sha256="0" * 64),
                "draft_translation does not match its SHA-256 digest",
            ),
            (
                replace(_segment(), final_translation_sha256="0" * 64),
                "effective_translation does not match its SHA-256 digest",
            ),
        )
        for segment, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    ApprovedTranslationValidationError,
                    message,
                ):
                    segment.validate()

    def test_from_dict_requires_an_object_and_every_field(self) -> None:
        with self.assertRaisesRegex(
            ApprovedTranslationValidationError,
            "must be a JSON object",
        ):
            ApprovedTranslationSegment.from_dict([])

        payload = _segment().to_dict()
        del payload["model"]
        with self.assertRaisesRegex(
            ApprovedTranslationValidationError,
            "missing fields: model",
        ):
            ApprovedTranslationSegment.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
