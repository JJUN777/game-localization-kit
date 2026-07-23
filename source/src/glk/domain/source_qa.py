"""Deterministic source QA issue model."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SOURCE_QA_SCHEMA_VERSION = 1
QA_SEVERITIES = {"error", "warning", "info"}
_ISSUE_ID_PATTERN = re.compile(r"^qa-[a-f0-9]{16}$")


class SourceQaValidationError(ValueError):
    """Raised when a source QA issue is malformed."""


@dataclass(frozen=True, slots=True)
class SourceQaIssue:
    schema_version: int
    id: str
    block_id: str
    severity: str
    code: str
    message: str
    evidence: str
    source_file: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    auto_fixable: bool = False

    def validate(self) -> None:
        if self.schema_version != SOURCE_QA_SCHEMA_VERSION:
            raise SourceQaValidationError(
                f"Unsupported source QA schema: {self.schema_version}"
            )
        if not isinstance(self.id, str) or not _ISSUE_ID_PATTERN.fullmatch(self.id):
            raise SourceQaValidationError(f"Invalid QA issue ID: {self.id!r}")
        if not isinstance(self.block_id, str) or not self.block_id:
            raise SourceQaValidationError("block_id cannot be empty.")
        if self.severity not in QA_SEVERITIES:
            raise SourceQaValidationError(f"Invalid QA severity: {self.severity!r}")
        if not isinstance(self.code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", self.code):
            raise SourceQaValidationError(f"Invalid QA code: {self.code!r}")
        for field_name, value in (
            ("message", self.message),
            ("evidence", self.evidence),
            ("source_file", self.source_file),
        ):
            if not isinstance(value, str) or not value:
                raise SourceQaValidationError(f"{field_name} cannot be empty.")
        if self.page is not None and (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page <= 0
        ):
            raise SourceQaValidationError("page must be a positive integer or null.")
        if self.bbox is not None and (
            not isinstance(self.bbox, tuple)
            or len(self.bbox) != 4
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in self.bbox
            )
        ):
            raise SourceQaValidationError("bbox must contain four numbers or be null.")
        if not isinstance(self.auto_fixable, bool):
            raise SourceQaValidationError("auto_fixable must be boolean.")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "block_id": self.block_id,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
            "source_file": self.source_file,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "auto_fixable": self.auto_fixable,
        }
