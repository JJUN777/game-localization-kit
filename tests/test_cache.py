from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from glk.application._cache import (
    CacheCorruptionError,
    CacheReadError,
    read_json_object,
)
from glk.application._hashing import sha256_file_if_exists


class JsonCacheTests(unittest.TestCase):
    def test_missing_cache_is_a_normal_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.json"

            self.assertIsNone(read_json_object(path))
            self.assertIsNone(sha256_file_if_exists(path))

    def test_malformed_or_non_object_json_is_reported_as_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(
                CacheCorruptionError,
                "invalid UTF-8 JSON",
            ):
                read_json_object(path)

            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(
                CacheCorruptionError,
                "must be an object",
            ):
                read_json_object(path)

    def test_storage_read_error_is_not_treated_as_a_cache_miss(self) -> None:
        path = Path("unreadable-cache.json")
        with patch.object(
            Path,
            "read_bytes",
            side_effect=PermissionError("permission denied"),
        ):
            with self.assertRaisesRegex(CacheReadError, "Could not read cache"):
                read_json_object(path)

        with patch.object(
            Path,
            "open",
            side_effect=PermissionError("permission denied"),
        ):
            with self.assertRaises(PermissionError):
                sha256_file_if_exists(path)


if __name__ == "__main__":
    unittest.main()
