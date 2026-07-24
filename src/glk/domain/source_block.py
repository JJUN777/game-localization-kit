"""Provider-independent source block model used by downstream pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SOURCE_BLOCK_SCHEMA_VERSION = 1
SOURCE_TYPES = {"pdf", "image"}
SOURCE_STATUSES = {"raw", "flagged", "corrected", "approved"}
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


class SourceBlockValidationError(ValueError):
    """Raised when an intermediate review-source block is malformed."""


@dataclass(frozen=True, slots=True)
class SourceBlock:
    schema_version: int
    id: str
    source_type: str
    source_file: str
    page: int | None
    source_order: int
    block_order: int
    block_type: str
    raw_text: str
    corrected_text: str | None
    bbox: tuple[float, float, float, float] | None
    legibility: str | None
    status: str
    warnings: tuple[str, ...]
    source_refs: tuple[str, ...]
    source_hash: str

    def validate(self) -> None:
        if self.schema_version != SOURCE_BLOCK_SCHEMA_VERSION:
            raise SourceBlockValidationError(
                f"Unsupported source block schema: {self.schema_version}"
            )
        if not isinstance(self.id, str) or not _ID_PATTERN.fullmatch(self.id):
            raise SourceBlockValidationError(f"Invalid source block ID: {self.id!r}")
        if self.source_type not in SOURCE_TYPES:
            raise SourceBlockValidationError(
                f"Invalid source type: {self.source_type!r}"
            )
        if not isinstance(self.source_file, str) or not self.source_file.strip():
            raise SourceBlockValidationError("source_file cannot be empty.")
        if self.page is not None and (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page <= 0
        ):
            raise SourceBlockValidationError("page must be a positive integer or null.")
        for field_name, value in (
            ("source_order", self.source_order),
            ("block_order", self.block_order),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SourceBlockValidationError(
                    f"{field_name} must be a positive integer."
                )
        if not isinstance(self.block_type, str) or not self.block_type.strip():
            raise SourceBlockValidationError("block_type cannot be empty.")
        if not isinstance(self.raw_text, str) or not self.raw_text.strip():
            raise SourceBlockValidationError("raw_text cannot be empty.")
        if self.corrected_text is not None and not isinstance(self.corrected_text, str):
            raise SourceBlockValidationError("corrected_text must be a string or null.")
        if self.bbox is not None:
            if not isinstance(self.bbox, tuple) or len(self.bbox) != 4 or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in self.bbox
            ):
                raise SourceBlockValidationError("bbox must contain four numbers.")
            x0, y0, x1, y1 = (float(value) for value in self.bbox)
            if not all(0 <= value <= 1000 for value in (x0, y0, x1, y1)):
                raise SourceBlockValidationError("bbox values must be normalized to 0..1000.")
            if x0 > x1 or y0 > y1:
                raise SourceBlockValidationError("bbox coordinates are reversed.")
        if self.legibility is not None and self.legibility not in {"clear", "uncertain"}:
            raise SourceBlockValidationError(
                "legibility must be clear, uncertain, or null."
            )
        if self.status not in SOURCE_STATUSES:
            raise SourceBlockValidationError(f"Invalid source status: {self.status!r}")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(value, str) for value in self.warnings
        ):
            raise SourceBlockValidationError("warnings must contain only strings.")
        if not isinstance(self.source_refs, tuple) or not all(
            isinstance(value, str) and value for value in self.source_refs
        ):
            raise SourceBlockValidationError("source_refs must contain non-empty strings.")
        if not isinstance(self.source_hash, str) or not _SHA256_PATTERN.fullmatch(
            self.source_hash
        ):
            raise SourceBlockValidationError("source_hash must be a prefixed SHA-256.")

    @property
    def effective_text(self) -> str:
        return self.corrected_text if self.corrected_text is not None else self.raw_text

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_type": self.source_type,
            "source_file": self.source_file,
            "page": self.page,
            "source_order": self.source_order,
            "block_order": self.block_order,
            "block_type": self.block_type,
            "raw_text": self.raw_text,
            "corrected_text": self.corrected_text,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "legibility": self.legibility,
            "status": self.status,
            "warnings": list(self.warnings),
            "source_refs": list(self.source_refs),
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SourceBlock:
        if not isinstance(value, dict):
            raise SourceBlockValidationError("Source block must be a JSON object.")
        required = {
            "schema_version",
            "id",
            "source_type",
            "source_file",
            "page",
            "source_order",
            "block_order",
            "block_type",
            "raw_text",
            "corrected_text",
            "bbox",
            "legibility",
            "status",
            "warnings",
            "source_refs",
            "source_hash",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise SourceBlockValidationError(
                f"Source block is missing fields: {', '.join(missing)}"
            )
        bbox_value = value["bbox"]
        block = cls(
            schema_version=value["schema_version"],
            id=value["id"],
            source_type=value["source_type"],
            source_file=value["source_file"],
            page=value["page"],
            source_order=value["source_order"],
            block_order=value["block_order"],
            block_type=value["block_type"],
            raw_text=value["raw_text"],
            corrected_text=value["corrected_text"],
            bbox=tuple(bbox_value) if isinstance(bbox_value, list) else bbox_value,
            legibility=value["legibility"],
            status=value["status"],
            warnings=(
                tuple(value["warnings"])
                if isinstance(value["warnings"], list)
                else value["warnings"]
            ),
            source_refs=(
                tuple(value["source_refs"])
                if isinstance(value["source_refs"], list)
                else value["source_refs"]
            ),
            source_hash=value["source_hash"],
        )
        block.validate()
        return block
