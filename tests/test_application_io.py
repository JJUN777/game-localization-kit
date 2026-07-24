from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from glk.application._hashing import sha256_bytes, sha256_file, sha256_file_if_exists
from glk.application._io import (
    copy_file_atomic,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)


class ApplicationIoTests(unittest.TestCase):
    def test_atomic_writers_create_parents_and_leave_no_temporary_files(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bytes_path = root / "nested/data.bin"
            text_path = root / "nested/data.txt"
            json_path = root / "nested/data.json"

            write_bytes_atomic(bytes_path, b"bytes")
            write_text_atomic(text_path, "text")
            write_json_atomic(json_path, {"name": "한글"})

            self.assertEqual(bytes_path.read_bytes(), b"bytes")
            self.assertEqual(text_path.read_text(encoding="utf-8"), "text\n")
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8")),
                {"name": "한글"},
            )
            self.assertEqual(list((root / "nested").glob("*.tmp")), [])
            self.assertEqual(list((root / "nested").glob(".*.tmp")), [])

    def test_atomic_copy_preserves_source_and_replaces_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            destination = root / "nested/destination.bin"
            source.write_bytes(b"new")
            write_bytes_atomic(destination, b"old")

            copy_file_atomic(source, destination)

            self.assertEqual(source.read_bytes(), b"new")
            self.assertEqual(destination.read_bytes(), b"new")

    def test_failed_replace_cleans_temporary_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "data.bin"

            with patch(
                "glk.application._io.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_bytes_atomic(destination, b"value")

            self.assertFalse(destination.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_hashing_helpers_share_file_and_byte_semantics(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "value.bin"
            path.write_bytes(b"value")

            expected = sha256_bytes(b"value")
            self.assertEqual(sha256_file(path), expected)
            self.assertEqual(sha256_file_if_exists(path), expected)
            self.assertIsNone(sha256_file_if_exists(path.with_name("missing.bin")))


if __name__ == "__main__":
    unittest.main()
