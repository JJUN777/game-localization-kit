"""Shared atomic filesystem writes for application services."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


def _temporary_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(name)


def _replace_from_temporary(path: Path, temporary_path: Path) -> None:
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, value: bytes) -> None:
    """Write bytes through a unique sibling temporary file."""
    temporary_path = _temporary_path(path)
    try:
        with temporary_path.open("wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        _replace_from_temporary(path, temporary_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, value: str) -> None:
    """Write UTF-8 text with one trailing newline when non-empty."""
    text = value if not value or value.endswith("\n") else value + "\n"
    write_bytes_atomic(path, text.encode("utf-8"))


def write_json_atomic(path: Path, value: Any) -> None:
    """Write human-readable UTF-8 JSON with a trailing newline."""
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_bytes_atomic(path, data)


def copy_file_atomic(source: Path, destination: Path) -> None:
    """Copy a file through a unique sibling temporary file."""
    temporary_path = _temporary_path(destination)
    try:
        shutil.copyfile(source, temporary_path)
        with temporary_path.open("rb+") as file:
            os.fsync(file.fileno())
        _replace_from_temporary(destination, temporary_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
