"""Shared JSON cache loading with explicit miss and failure semantics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CacheCorruptionError(ValueError):
    """Raised when an existing JSON cache cannot be decoded or validated."""


class CacheReadError(OSError):
    """Raised when an existing cache cannot be read from storage."""


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Read an optional JSON object without hiding storage or corruption errors."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CacheReadError(f"Could not read cache file {path}: {error}") from error

    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CacheCorruptionError(
            f"Cache file contains invalid UTF-8 JSON: {path}"
        ) from error
    if not isinstance(value, dict):
        raise CacheCorruptionError(f"Cache JSON must be an object: {path}")
    return value


def invalid_cache(path: Path, detail: str) -> CacheCorruptionError:
    """Build a consistent error for a structurally invalid cache object."""
    return CacheCorruptionError(f"Cache file is invalid ({detail}): {path}")
