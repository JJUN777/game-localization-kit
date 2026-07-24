"""Project manifest domain model and validation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


PROJECT_SCHEMA_VERSION = 3
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class ProjectError(ValueError):
    """Base error for invalid or unavailable projects."""


class ProjectValidationError(ProjectError):
    """Raised when a project manifest or identifier is invalid."""


def normalize_project_id(value: str) -> str:
    """Convert a human-readable name into a portable workspace directory name."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        raise ProjectValidationError(
            "Project ID is empty after normalization; provide an ID using lowercase "
            "English letters, numbers, and underscores."
        )
    if len(normalized) > 80:
        raise ProjectValidationError("Project ID must be 80 characters or fewer.")
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        raise ProjectValidationError(f"Project ID is reserved on Windows: {normalized}")
    return normalized


def _validate_language_code(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _LANGUAGE_PATTERN.fullmatch(value):
        raise ProjectValidationError(
            f"{field_name} must be a language code such as 'en', 'ko', or 'pt-BR'."
        )


def _validate_source_file(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ProjectValidationError("source_file must be a relative path or null.")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ProjectValidationError("source_file must be a relative path inside the workspace.")


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    schema_version: int
    project_id: str
    name: str
    profile: str
    source_language: str
    target_language: str
    source_file: str | None
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        project_id: str | None = None,
        profile: str = "default",
        source_language: str = "en",
        target_language: str = "ko",
    ) -> ProjectManifest:
        clean_name = name.strip()
        if not clean_name:
            raise ProjectValidationError("Project name cannot be empty.")
        if len(clean_name) > 200:
            raise ProjectValidationError("Project name must be 200 characters or fewer.")
        if project_id is None:
            normalized_id = normalize_project_id(clean_name)
        else:
            normalized_id = unicodedata.normalize("NFKC", project_id).strip()
            if not _PROJECT_ID_PATTERN.fullmatch(normalized_id):
                raise ProjectValidationError(
                    "Project ID must use lowercase English letters, numbers, and "
                    "single underscores only."
                )
            normalized_id = normalize_project_id(normalized_id)
        manifest = cls(
            schema_version=PROJECT_SCHEMA_VERSION,
            project_id=normalized_id,
            name=clean_name,
            profile=profile.strip() or "default",
            source_language=source_language,
            target_language=target_language,
            source_file=None,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
        )
        manifest.validate()
        return manifest

    @classmethod
    def from_dict(cls, value: Any) -> ProjectManifest:
        if not isinstance(value, dict):
            raise ProjectValidationError("project.json must contain a JSON object.")
        required_fields = {field.name for field in cls.__dataclass_fields__.values()}
        missing = sorted(required_fields - value.keys())
        if missing:
            raise ProjectValidationError(
                f"project.json is missing required fields: {', '.join(missing)}"
            )
        manifest = cls(**{field: value[field] for field in required_fields})
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise ProjectValidationError("schema_version must be an integer.")
        if self.schema_version != PROJECT_SCHEMA_VERSION:
            raise ProjectValidationError(
                f"Unsupported project schema version: {self.schema_version}"
            )
        if not isinstance(self.project_id, str):
            raise ProjectValidationError("project_id must be a string.")
        if self.project_id != normalize_project_id(self.project_id):
            raise ProjectValidationError("project_id is not in normalized form.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProjectValidationError("Project name cannot be empty.")
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ProjectValidationError("Project profile cannot be empty.")
        _validate_language_code(self.source_language, "source_language")
        _validate_language_code(self.target_language, "target_language")
        _validate_source_file(self.source_file)
        if not isinstance(self.created_at, str):
            raise ProjectValidationError("created_at must be an ISO-8601 string.")
        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProjectValidationError("created_at must be an ISO-8601 string.") from error

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_source_file(self, source_file: str) -> ProjectManifest:
        updated = replace(self, source_file=source_file)
        updated.validate()
        return updated
