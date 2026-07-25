from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from glk.application._hashing import sha256_bytes, sha256_file, sha256_file_if_exists
from glk.application._io import (
    _fsync_parent,
    append_bytes_durable,
    copy_file_atomic,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)


class ApplicationIoTests(unittest.TestCase):
    @staticmethod
    def _close_except_fake(fake_descriptor: int):
        real_close = os.close

        def close(descriptor: int) -> None:
            if descriptor != fake_descriptor:
                real_close(descriptor)

        return close

    def test_durable_append_creates_parent_and_preserves_existing_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested/events.jsonl"

            append_bytes_durable(path, b"first\n")
            append_bytes_durable(path, b"second\n")

            self.assertEqual(path.read_bytes(), b"first\nsecond\n")

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

    def test_parent_fsync_ignores_unsupported_open_error(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.bin"
            unsupported = OSError(
                errno.EINVAL,
                "directory open is unsupported",
            )
            with (
                patch("glk.application._io.os.name", "posix"),
                patch(
                    "glk.application._io._open_parent_directory",
                    side_effect=unsupported,
                ),
                patch("glk.application._io.os.fsync") as fsync,
            ):
                _fsync_parent(path)

            fsync.assert_not_called()

    def test_atomic_replace_ignores_unsupported_directory_fsync(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.bin"
            fake_descriptor = 987_001
            fsync_calls = 0

            def fsync(_descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError(
                        errno.EINVAL,
                        "directory fsync is unsupported",
                    )

            with (
                patch("glk.application._io.os.name", "posix"),
                patch(
                    "glk.application._io._open_parent_directory",
                    return_value=fake_descriptor,
                ),
                patch(
                    "glk.application._io.os.close",
                    side_effect=self._close_except_fake(fake_descriptor),
                ),
                patch("glk.application._io.os.fsync", side_effect=fsync),
            ):
                write_bytes_atomic(path, b"replaced")

            self.assertEqual(path.read_bytes(), b"replaced")
            self.assertEqual(fsync_calls, 2)

    def test_new_append_ignores_unsupported_directory_fsync(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            fake_descriptor = 987_002
            fsync_calls = 0
            unsupported_errno = getattr(
                errno,
                "ENOTSUP",
                errno.EINVAL,
            )

            def fsync(_descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError(
                        unsupported_errno,
                        "directory fsync is unsupported",
                    )

            with (
                patch("glk.application._io.os.name", "posix"),
                patch(
                    "glk.application._io._open_parent_directory",
                    return_value=fake_descriptor,
                ),
                patch(
                    "glk.application._io.os.close",
                    side_effect=self._close_except_fake(fake_descriptor),
                ),
                patch("glk.application._io.os.fsync", side_effect=fsync),
            ):
                append_bytes_durable(path, b"first\n")

            self.assertEqual(path.read_bytes(), b"first\n")
            self.assertEqual(fsync_calls, 2)

    def test_directory_io_error_propagates_after_atomic_replace(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.bin"
            fake_descriptor = 987_003
            fsync_calls = 0

            def fsync(_descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError(errno.EIO, "directory storage failure")

            with (
                patch("glk.application._io.os.name", "posix"),
                patch(
                    "glk.application._io._open_parent_directory",
                    return_value=fake_descriptor,
                ),
                patch(
                    "glk.application._io.os.close",
                    side_effect=self._close_except_fake(fake_descriptor),
                ),
                patch("glk.application._io.os.fsync", side_effect=fsync),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "directory storage failure",
                ):
                    write_bytes_atomic(path, b"replaced")

            self.assertEqual(path.read_bytes(), b"replaced")
            self.assertEqual(fsync_calls, 2)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_parent_fsync_propagates_open_permission_error(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.bin"
            with (
                patch("glk.application._io.os.name", "posix"),
                patch(
                    "glk.application._io._open_parent_directory",
                    side_effect=PermissionError(
                        errno.EACCES,
                        "directory permission denied",
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "directory permission denied",
                ):
                    _fsync_parent(path)

    def test_file_fsync_error_prevents_atomic_replace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data.bin"
            with (
                patch(
                    "glk.application._io.os.fsync",
                    side_effect=OSError(errno.EIO, "file storage failure"),
                ),
                patch("glk.application._io.os.replace") as replace,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "file storage failure",
                ):
                    write_bytes_atomic(path, b"value")

            replace.assert_not_called()
            self.assertFalse(path.exists())
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
