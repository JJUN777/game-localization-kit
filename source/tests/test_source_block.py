from __future__ import annotations

import unittest

from glk.domain.source_block import (
    SOURCE_BLOCK_SCHEMA_VERSION,
    SourceBlock,
    SourceBlockValidationError,
)


class SourceBlockTests(unittest.TestCase):
    def create_block(self) -> SourceBlock:
        return SourceBlock(
            schema_version=SOURCE_BLOCK_SCHEMA_VERSION,
            id="pdf-p0001-b0001-1234567890",
            source_type="pdf",
            source_file="02_source/assets/original.pdf",
            page=1,
            source_order=1,
            block_order=1,
            block_type="paragraph",
            raw_text="Original text.",
            corrected_text=None,
            bbox=(10.0, 20.0, 900.0, 950.0),
            legibility=None,
            status="raw",
            warnings=(),
            source_refs=("P001-F001",),
            source_hash="sha256:" + "a" * 64,
        )

    def test_round_trip_and_effective_text(self) -> None:
        block = self.create_block()
        restored = SourceBlock.from_dict(block.to_dict())
        self.assertEqual(restored, block)
        self.assertEqual(restored.effective_text, "Original text.")

    def test_corrected_text_is_effective_without_changing_raw_text(self) -> None:
        value = self.create_block().to_dict()
        value["corrected_text"] = "Corrected text."
        value["status"] = "corrected"
        block = SourceBlock.from_dict(value)
        self.assertEqual(block.raw_text, "Original text.")
        self.assertEqual(block.effective_text, "Corrected text.")

    def test_rejects_non_normalized_bbox(self) -> None:
        value = self.create_block().to_dict()
        value["bbox"] = [0, 0, 1001, 1000]
        with self.assertRaises(SourceBlockValidationError):
            SourceBlock.from_dict(value)


if __name__ == "__main__":
    unittest.main()
