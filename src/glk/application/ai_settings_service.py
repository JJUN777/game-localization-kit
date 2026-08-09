"""Read and update local AI provider settings without exposing API keys."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from dotenv import dotenv_values

from glk.application._io import write_text_atomic
from glk.config import resolve_settings_root
from glk.infrastructure.ai_provider import AI_PROVIDER_NAMES, DEFAULT_AI_PROVIDER
from glk.infrastructure.gemini_common import DEFAULT_MODEL as DEFAULT_GEMINI_MODEL
from glk.infrastructure.openai_common import DEFAULT_OPENAI_MODEL


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_SETTING_NAMES = (
    "GLK_AI_PROVIDER",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
)
_SETTING_LINE = re.compile(
    r"^\s*(?:export\s+)?(" + "|".join(_SETTING_NAMES) + r")\s*=",
)
_PROVIDER_SETTINGS = {
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
}


class AiSettingsError(ValueError):
    """Raised when local AI settings cannot be read or saved safely."""


@dataclass(frozen=True, slots=True)
class AiSettingsStatus:
    provider: str
    provider_source: str
    api_key_configured: bool
    api_key_source: str
    model: str
    model_source: str
    env_file: str
    environment_override: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AiSettingsService:
    """Manage repository-local provider, credential, and model settings."""

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
        self._environment = {
            name: source_environment.get(name, "").strip()
            for name in _SETTING_NAMES
        }

    def _file_values(self) -> dict[str, str]:
        if not self.env_path.is_file():
            return {}
        try:
            parsed = dotenv_values(self.env_path)
        except (OSError, UnicodeError, ValueError) as error:
            raise AiSettingsError(
                f"Unable to read AI settings from {self.env_path}."
            ) from error
        return {
            name: value.strip()
            for name in _SETTING_NAMES
            if isinstance((value := parsed.get(name)), str) and value.strip()
        }

    @staticmethod
    def _validate_provider(value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in AI_PROVIDER_NAMES:
            raise AiSettingsError("AI provider must be gemini or openai.")
        return normalized

    def status(self) -> AiSettingsStatus:
        values = self._file_values()
        environment_provider = self._environment["GLK_AI_PROVIDER"]
        file_provider = values.get("GLK_AI_PROVIDER", "")
        if environment_provider:
            provider = self._validate_provider(environment_provider)
            provider_source = "environment"
        elif file_provider:
            provider = self._validate_provider(file_provider)
            provider_source = "env_file"
        else:
            provider = DEFAULT_AI_PROVIDER
            provider_source = "default"

        api_key_name, model_name, default_model = _PROVIDER_SETTINGS[provider]
        environment_api_key = self._environment[api_key_name]
        environment_model = self._environment[model_name]
        file_api_key = values.get(api_key_name, "")
        file_model = values.get(model_name, "")
        if environment_api_key:
            api_key_source = "environment"
        elif file_api_key:
            api_key_source = "env_file"
        else:
            api_key_source = "missing"
        if environment_model:
            model = environment_model
            model_source = "environment"
        elif file_model:
            model = file_model
            model_source = "env_file"
        else:
            model = default_model
            model_source = "default"

        overrides = {
            "api_key": bool(environment_api_key),
            "model": bool(environment_model),
        }
        if environment_provider:
            overrides["provider"] = True
        return AiSettingsStatus(
            provider=provider,
            provider_source=provider_source,
            api_key_configured=api_key_source != "missing",
            api_key_source=api_key_source,
            model=model,
            model_source=model_source,
            env_file=".env",
            environment_override=overrides,
        )

    @staticmethod
    def _validate_api_key(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AiSettingsError("API key must not be empty.")
        if (
            len(normalized) > 512
            or "\x00" in normalized
            or any(character.isspace() for character in normalized)
        ):
            raise AiSettingsError("API key format is invalid.")
        return normalized

    @staticmethod
    def _validate_model(value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 200
            or not _MODEL_NAME.fullmatch(normalized)
        ):
            raise AiSettingsError("AI model name format is invalid.")
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
                f"Unable to read AI settings from {self.env_path}."
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
        provider: str | None = None,
    ) -> AiSettingsStatus:
        selected_provider = self._validate_provider(
            provider if provider is not None else self.status().provider
        )
        api_key_name, model_name, _ = _PROVIDER_SETTINGS[selected_provider]
        updates = {
            "GLK_AI_PROVIDER": selected_provider,
            model_name: self._validate_model(model),
        }
        if api_key is not None and api_key.strip():
            updates[api_key_name] = self._validate_api_key(api_key)

        self.settings_root.mkdir(parents=True, exist_ok=True)
        try:
            write_text_atomic(self.env_path, self._updated_env_text(updates))
            if os.name != "nt":
                self.env_path.chmod(0o600)
        except OSError as error:
            raise AiSettingsError(
                f"Unable to save AI settings to {self.env_path}."
            ) from error
        return self.status()
