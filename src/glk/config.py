"""Stable per-user configuration paths shared by CLI and providers."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping


SETTINGS_ROOT_ENV = "GLK_SETTINGS_ROOT"
_APPLICATION_DIRECTORY = "game-localization-kit"


def _editable_project_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (
        (candidate / "pyproject.toml").is_file()
        and (candidate / "src/glk").is_dir()
    ):
        return candidate
    return None


def _user_settings_root(
    environment: Mapping[str, str],
    *,
    home: Path,
    platform: str,
) -> Path:
    if platform == "win32":
        app_data = environment.get("APPDATA", "").strip()
        base = Path(app_data).expanduser() if app_data else home / "AppData/Roaming"
    elif platform == "darwin":
        base = home / "Library/Application Support"
    else:
        xdg_config = environment.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg_config).expanduser() if xdg_config else home / ".config"
    return base / _APPLICATION_DIRECTORY


def resolve_settings_root(
    settings_root: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    platform: str | None = None,
    editable_root: Path | None = None,
    detect_editable_root: bool = True,
) -> Path:
    """Resolve one settings root independently from the process working directory."""
    active_environment = os.environ if environment is None else environment
    working_directory = Path.cwd() if cwd is None else cwd
    selected: str | Path | None = settings_root
    if selected is None:
        configured = active_environment.get(SETTINGS_ROOT_ENV, "").strip()
        selected = configured or None
    if selected is not None:
        path = Path(selected).expanduser()
        if not path.is_absolute():
            path = working_directory / path
        return path.resolve()

    source_root = editable_root
    if source_root is None and detect_editable_root:
        source_root = _editable_project_root()
    if source_root is not None:
        return source_root.resolve()
    user_root = _user_settings_root(
        active_environment,
        home=Path.home() if home is None else home,
        platform=sys.platform if platform is None else platform,
    )
    return user_root.expanduser().resolve()
