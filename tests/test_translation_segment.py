from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest

from glk.domain.translation_segment import (
    TRANSLATION_SEGMENT_SCHEMA_VERSION,
    TranslationSegment,
    TranslationSegmentValidationError,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _segment() -> TranslationSegment:
    source_text = "Gain {count} points."
    translated_text = "{count}점을 얻습니다."
    return TranslationSegment(
        schema_version=TRANSLATION_SEGMENT_SCHEMA_VERSION,
        source_block_id="pdf-p0001-b0001",
        source_file="01_input/pdf/rulebook.pdf",
        page=1,
        source_order=1,
        block_type="paragraph",
        source_text=source_text,
        source_sha256=_digest(source_text),
        translated_text=translated_text,
        translation_sha256=_digest(translated_text),
        status="translated",
        model="test-model",
        prompt_sha256="a" * 64,
        termbase_sha256="b" * 64,
    )


class TranslationSegmentTests(unittest.TestCase):
    def test_round_trips_a_valid_segment(self) -> None:
        segment = _segment()

        payload = segment.to_dict()

        self.assertEqual(TranslationSegment.from_dict(payload), segment)

    def test_rejects_invalid_schema_identity_and_status_fields(self) -> None:
        cases = (
            (
                replace(_segment(), schema_version=999),
                "Unsupported translation segment schema",
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
                replace(_segment(), translated_text=" "),
                "translated_text cannot be empty",
            ),
            (
                replace(_segment(), status="approved"),
                "Invalid translation status",
            ),
        )
        for segment, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    TranslationSegmentValidationError,
                    message,
                ):
                    segment.validate()

    def test_rejects_invalid_or_mismatched_hashes(self) -> None:
        cases = (
            (
                replace(_segment(), prompt_sha256="not-a-digest"),
                "prompt_sha256 must be a SHA-256 hex digest",
            ),
            (
                replace(_segment(), source_sha256="0" * 64),
                "source_text does not match source_sha256",
            ),
            (
                replace(_segment(), translation_sha256="0" * 64),
                "translated_text does not match translation_sha256",
            ),
        )
        for segment, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    TranslationSegmentValidationError,
                    message,
                ):
                    segment.validate()

    def test_from_dict_requires_an_object_and_every_field(self) -> None:
        with self.assertRaisesRegex(
            TranslationSegmentValidationError,
            "must be a JSON object",
        ):
            TranslationSegment.from_dict([])

        payload = _segment().to_dict()
        del payload["model"]
        with self.assertRaisesRegex(
            TranslationSegmentValidationError,
            "missing fields: model",
        ):
            TranslationSegment.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
