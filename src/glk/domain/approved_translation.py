"""Final human-approved translation linked to its machine draft and source."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


APPROVED_TRANSLATION_SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ApprovedTranslationValidationError(ValueError):
    """Raised when an approved translation segment is malformed."""


@dataclass(frozen=True, slots=True)
class ApprovedTranslationSegment:
    schema_version: int
    source_block_id: str
    source_file: str
    page: int | None
    source_order: int
    block_type: str
    source_text: str
    source_sha256: str
    draft_translation: str
    draft_translation_sha256: str
    corrected_translation: str | None
    final_translation_sha256: str
    status: str
    model: str
    prompt_sha256: str
    termbase_sha256: str

    @property
    def effective_translation(self) -> str:
        return self.corrected_translation or self.draft_translation

    def validate(self) -> None:
        if self.schema_version != APPROVED_TRANSLATION_SCHEMA_VERSION:
            raise ApprovedTranslationValidationError(
                f"Unsupported approved translation schema: {self.schema_version}"
            )
        if not isinstance(self.source_block_id, str) or not _ID_PATTERN.fullmatch(
            self.source_block_id
        ):
            raise ApprovedTranslationValidationError(
                f"Invalid source block ID: {self.source_block_id!r}"
            )
        if not isinstance(self.source_file, str) or not self.source_file:
            raise ApprovedTranslationValidationError("source_file cannot be empty.")
        if self.page is not None and (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page <= 0
        ):
            raise ApprovedTranslationValidationError(
                "page must be a positive integer or null."
            )
        if (
            not isinstance(self.source_order, int)
            or isinstance(self.source_order, bool)
            or self.source_order <= 0
        ):
            raise ApprovedTranslationValidationError(
                "source_order must be a positive integer."
            )
        for field_name, value in (
            ("block_type", self.block_type),
            ("source_text", self.source_text),
            ("draft_translation", self.draft_translation),
            ("model", self.model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ApprovedTranslationValidationError(
                    f"{field_name} cannot be empty."
                )
        if self.corrected_translation is not None and (
            not isinstance(self.corrected_translation, str)
            or not self.corrected_translation.strip()
        ):
            raise ApprovedTranslationValidationError(
                "corrected_translation must be null or non-empty."
            )
        if self.corrected_translation == self.draft_translation:
            raise ApprovedTranslationValidationError(
                "Unchanged text must use null corrected_translation."
            )
        for field_name, value in (
            ("source_sha256", self.source_sha256),
            ("draft_translation_sha256", self.draft_translation_sha256),
            ("final_translation_sha256", self.final_translation_sha256),
            ("prompt_sha256", self.prompt_sha256),
            ("termbase_sha256", self.termbase_sha256),
        ):
            if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
                raise ApprovedTranslationValidationError(
                    f"{field_name} must be a SHA-256 hex digest."
                )
        expected_hashes = (
            ("source_text", self.source_text, self.source_sha256),
            (
                "draft_translation",
                self.draft_translation,
                self.draft_translation_sha256,
            ),
            (
                "effective_translation",
                self.effective_translation,
                self.final_translation_sha256,
            ),
        )
        for field_name, text, expected in expected_hashes:
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
                raise ApprovedTranslationValidationError(
                    f"{field_name} does not match its SHA-256 digest."
                )
        if self.status != "approved":
            raise ApprovedTranslationValidationError(
                f"Invalid approved translation status: {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }

    @classmethod
    def from_dict(cls, value: Any) -> ApprovedTranslationSegment:
        if not isinstance(value, dict):
            raise ApprovedTranslationValidationError(
                "Approved translation segment must be a JSON object."
            )
        required = set(cls.__dataclass_fields__)
        missing = sorted(required - value.keys())
        if missing:
            raise ApprovedTranslationValidationError(
                "Approved translation segment is missing fields: "
                + ", ".join(missing)
            )
        segment = cls(**{field: value[field] for field in required})
        segment.validate()
        return segment
