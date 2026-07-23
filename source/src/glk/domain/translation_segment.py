"""Validated translated segment linked to an approved source block."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


TRANSLATION_SEGMENT_SCHEMA_VERSION = 1
TRANSLATION_STATUSES = {"translated", "flagged"}
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class TranslationSegmentValidationError(ValueError):
    """Raised when a translated segment is malformed."""


@dataclass(frozen=True, slots=True)
class TranslationSegment:
    schema_version: int
    source_block_id: str
    source_file: str
    page: int | None
    source_order: int
    block_type: str
    source_text: str
    source_sha256: str
    translated_text: str
    translation_sha256: str
    status: str
    model: str
    prompt_sha256: str
    termbase_sha256: str

    def validate(self) -> None:
        if self.schema_version != TRANSLATION_SEGMENT_SCHEMA_VERSION:
            raise TranslationSegmentValidationError(
                f"Unsupported translation segment schema: {self.schema_version}"
            )
        if not isinstance(self.source_block_id, str) or not _ID_PATTERN.fullmatch(
            self.source_block_id
        ):
            raise TranslationSegmentValidationError(
                f"Invalid source block ID: {self.source_block_id!r}"
            )
        if not isinstance(self.source_file, str) or not self.source_file:
            raise TranslationSegmentValidationError("source_file cannot be empty.")
        if self.page is not None and (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page <= 0
        ):
            raise TranslationSegmentValidationError(
                "page must be a positive integer or null."
            )
        if (
            not isinstance(self.source_order, int)
            or isinstance(self.source_order, bool)
            or self.source_order <= 0
        ):
            raise TranslationSegmentValidationError(
                "source_order must be a positive integer."
            )
        for field_name, value in (
            ("block_type", self.block_type),
            ("source_text", self.source_text),
            ("translated_text", self.translated_text),
            ("model", self.model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise TranslationSegmentValidationError(
                    f"{field_name} cannot be empty."
                )
        for field_name, value in (
            ("source_sha256", self.source_sha256),
            ("translation_sha256", self.translation_sha256),
            ("prompt_sha256", self.prompt_sha256),
            ("termbase_sha256", self.termbase_sha256),
        ):
            if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
                raise TranslationSegmentValidationError(
                    f"{field_name} must be a SHA-256 hex digest."
                )
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise TranslationSegmentValidationError(
                "source_text does not match source_sha256."
            )
        if (
            hashlib.sha256(self.translated_text.encode("utf-8")).hexdigest()
            != self.translation_sha256
        ):
            raise TranslationSegmentValidationError(
                "translated_text does not match translation_sha256."
            )
        if self.status not in TRANSLATION_STATUSES:
            raise TranslationSegmentValidationError(
                f"Invalid translation status: {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "source_block_id": self.source_block_id,
            "source_file": self.source_file,
            "page": self.page,
            "source_order": self.source_order,
            "block_type": self.block_type,
            "source_text": self.source_text,
            "source_sha256": self.source_sha256,
            "translated_text": self.translated_text,
            "translation_sha256": self.translation_sha256,
            "status": self.status,
            "model": self.model,
            "prompt_sha256": self.prompt_sha256,
            "termbase_sha256": self.termbase_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TranslationSegment:
        if not isinstance(value, dict):
            raise TranslationSegmentValidationError(
                "Translation segment must be a JSON object."
            )
        required = {field.name for field in cls.__dataclass_fields__.values()}
        missing = sorted(required - value.keys())
        if missing:
            raise TranslationSegmentValidationError(
                "Translation segment is missing fields: " + ", ".join(missing)
            )
        segment = cls(**{field: value[field] for field in required})
        segment.validate()
        return segment
