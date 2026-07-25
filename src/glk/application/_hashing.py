"""Shared hashing helpers for application services."""

from __future__ import annotations

import hashlib
from pathlib import Path


class FileHashCache:
    """Reuse byte and normalized-text hashes within one read-only snapshot."""

    def __init__(self) -> None:
        self._byte_hashes: dict[Path, str | None] = {}
        self._text_hashes: dict[Path, str | None] = {}

    def sha256_file_if_exists(self, path: Path) -> str | None:
        candidate = Path(path)
        if candidate not in self._byte_hashes:
            self._byte_hashes[candidate] = sha256_file_if_exists(candidate)
        return self._byte_hashes[candidate]

    def sha256_text_file_if_exists(self, path: Path) -> str | None:
        candidate = Path(path)
        if candidate not in self._text_hashes:
            self._text_hashes[candidate] = sha256_text_file_if_exists(candidate)
        return self._text_hashes[candidate]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_text_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def sha256_text(value: str) -> str:
    return sha256_bytes(normalize_text_newlines(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_if_exists(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except FileNotFoundError:
        return None


def sha256_text_file_if_exists(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", newline=None) as file:
            return sha256_text(file.read())
    except FileNotFoundError:
        return None
