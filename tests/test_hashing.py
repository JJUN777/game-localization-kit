from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from glk.application._hashing import (
    sha256_text,
    sha256_text_file_if_exists,
)


class TextHashingTests(unittest.TestCase):
    def test_logically_equal_newlines_have_the_same_hash(self) -> None:
        self.assertEqual(
            sha256_text("first\nsecond\n"),
            sha256_text("first\r\nsecond\r\n"),
        )
        self.assertEqual(
            sha256_text("first\nsecond\n"),
            sha256_text("first\rsecond\r"),
        )

    def test_text_file_hash_normalizes_platform_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.txt"
            path.write_bytes(b"first\r\nsecond\r\n")

            self.assertEqual(
                sha256_text_file_if_exists(path),
                sha256_text("first\nsecond\n"),
            )


if __name__ == "__main__":
    unittest.main()
