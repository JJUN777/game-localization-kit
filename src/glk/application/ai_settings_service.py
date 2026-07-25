"""Read and update local Gemini settings without exposing API key values."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from dotenv import dotenv_values

from glk.application._io import write_text_atomic
from glk.config import resolve_settings_root
from glk.infrastructure.gemini_common import DEFAULT_MODEL


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_SETTING_LINE = re.compile(
    r"^\s*(?:export\s+)?(GEMINI_API_KEY|GEMINI_MODEL)\s*=",
)
_SETTING_NAMES = ("GEMINI_API_KEY", "GEMINI_MODEL")


class AiSettingsError(ValueError):
    """Raised when local Gemini settings cannot be read or saved safely."""


@dataclass(frozen=True, slots=True)
class AiSettingsStatus:
    api_key_configured: bool
    api_key_source: str
    model: str
    model_source: str
    env_file: str
    environment_override: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AiSettingsService:
    """Manage the dashboard process's repository-local Gemini settings."""

    def __init__(
        self,
        settings_root: str | Path | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.settings_root = resolve_settings_root(
            settings_root,
            environment=environment,
        )
        self.env_path = self.settings_root / ".env"
        source_environment = os.environ if environment is None else environment
        self._environment_api_key = source_environment.get(
            "GEMINI_API_KEY",
            "",
        ).strip()
        self._environment_model = source_environment.get(
            "GEMINI_MODEL",
            "",
        ).strip()

    def _file_values(self) -> dict[str, str]:
        if not self.env_path.is_file():
            return {}
        try:
            parsed = dotenv_values(self.env_path)
        except (OSError, UnicodeError, ValueError) as error:
            raise AiSettingsError(
                f"Unable to read Gemini settings from {self.env_path}."
            ) from error
        return {
            name: value.strip()
            for name in _SETTING_NAMES
            if isinstance((value := parsed.get(name)), str)
            and value.strip()
        }

    def status(self) -> AiSettingsStatus:
        values = self._file_values()
        file_api_key = values.get("GEMINI_API_KEY", "")
        file_model = values.get("GEMINI_MODEL", "")
        if self._environment_api_key:
            api_key_source = "environment"
        elif file_api_key:
            api_key_source = "env_file"
        else:
            api_key_source = "missing"
        if self._environment_model:
            model = self._environment_model
            model_source = "environment"
        elif file_model:
            model = file_model
            model_source = "env_file"
        else:
            model = DEFAULT_MODEL
            model_source = "default"
        return AiSettingsStatus(
            api_key_configured=api_key_source != "missing",
            api_key_source=api_key_source,
            model=model,
            model_source=model_source,
            env_file=".env",
            environment_override={
                "api_key": bool(self._environment_api_key),
                "model": bool(self._environment_model),
            },
        )

    @staticmethod
    def _validate_api_key(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AiSettingsError("Gemini API key must not be empty.")
        if (
            len(normalized) > 512
            or "\x00" in normalized
            or any(character.isspace() for character in normalized)
        ):
            raise AiSettingsError("Gemini API key format is invalid.")
        return normalized

    @staticmethod
    def _validate_model(value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 200
            or not _MODEL_NAME.fullmatch(normalized)
        ):
            raise AiSettingsError("Gemini model name format is invalid.")
        return normalized

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _updated_env_text(self, updates: Mapping[str, str]) -> str:
        try:
            original = (
                self.env_path.read_text(encoding="utf-8")
                if self.env_path.is_file()
                else ""
            )
        except (OSError, UnicodeError) as error:
            raise AiSettingsError(
                f"Unable to read Gemini settings from {self.env_path}."
            ) from error

        output: list[str] = []
        written: set[str] = set()
        for line in original.splitlines():
            match = _SETTING_LINE.match(line)
            if match is None or match.group(1) not in updates:
                output.append(line)
                continue
            name = match.group(1)
            if name not in written:
                output.append(f"{name}={self._quote(updates[name])}")
                written.add(name)
        for name in _SETTING_NAMES:
            if name in updates and name not in written:
                output.append(f"{name}={self._quote(updates[name])}")
        return "\n".join(output)

    def save(
        self,
        *,
        api_key: str | None,
        model: str,
    ) -> AiSettingsStatus:
        validated_model = self._validate_model(model)
        values = self._file_values()
        updates = {"GEMINI_MODEL": validated_model}
        if api_key is not None and api_key.strip():
            updates["GEMINI_API_KEY"] = self._validate_api_key(api_key)

        self.settings_root.mkdir(parents=True, exist_ok=True)
        try:
            write_text_atomic(
                self.env_path,
                self._updated_env_text(updates),
            )
            if os.name != "nt":
                self.env_path.chmod(0o600)
        except OSError as error:
            raise AiSettingsError(
                f"Unable to save Gemini settings to {self.env_path}."
            ) from error

        active_file_values = {**values, **updates}
        if not self._environment_api_key:
            file_api_key = active_file_values.get("GEMINI_API_KEY", "")
            if file_api_key:
                os.environ["GEMINI_API_KEY"] = file_api_key
        if not self._environment_model:
            os.environ["GEMINI_MODEL"] = validated_model
        return self.status()
