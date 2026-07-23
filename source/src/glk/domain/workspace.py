"""Canonical version-2 project workspace layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PDF_SOURCE_FILE = "02_source/assets/original.pdf"
IMAGE_SOURCE_ROOT = "02_source/assets/images"

WORKSPACE_DIRECTORIES = (
    Path("01_input/pdf"),
    Path("01_input/images"),
    Path("02_source/assets/images"),
    Path("02_source/ocr/individual"),
    Path("03_terminology"),
    Path("04_translation/revisions"),
    Path("05_output"),
    Path(".glk/cache/pdf/pages"),
    Path(".glk/cache/pdf/fragments"),
    Path(".glk/cache/pdf/layouts"),
    Path(".glk/cache/ocr/results"),
    Path(".glk/segments"),
    Path(".glk/state"),
    Path(".glk/reports"),
)


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path

    @property
    def input_pdf_dir(self) -> Path:
        return self.root / "01_input/pdf"

    @property
    def input_images_dir(self) -> Path:
        return self.root / "01_input/images"

    @property
    def source_dir(self) -> Path:
        return self.root / "02_source"

    @property
    def source_pdf(self) -> Path:
        return self.root / PDF_SOURCE_FILE

    @property
    def source_images(self) -> Path:
        return self.root / IMAGE_SOURCE_ROOT

    @property
    def source_ocr_prompt(self) -> Path:
        return self.root / "02_source/assets/ocr_prompt.txt"

    @property
    def pdf_pages(self) -> Path:
        return self.root / ".glk/cache/pdf/pages"

    @property
    def pdf_fragments(self) -> Path:
        return self.root / ".glk/cache/pdf/fragments"

    @property
    def pdf_layouts(self) -> Path:
        return self.root / ".glk/cache/pdf/layouts"

    @property
    def ocr_results(self) -> Path:
        return self.root / ".glk/cache/ocr/results"

    @property
    def ocr_individual(self) -> Path:
        return self.root / "02_source/ocr/individual"

    @property
    def ocr_combined(self) -> Path:
        return self.root / "02_source/ocr/combined.txt"

    @property
    def ocr_combined_partial(self) -> Path:
        return self.root / "02_source/ocr/combined.partial.txt"

    @property
    def source_extracted(self) -> Path:
        return self.root / "02_source/extracted.txt"

    @property
    def source_extracted_partial(self) -> Path:
        return self.root / "02_source/extracted.partial.txt"

    @property
    def source_draft(self) -> Path:
        return self.root / "02_source/draft.txt"

    @property
    def source_review(self) -> Path:
        return self.root / "02_source/review.txt"

    @property
    def source_qa_markdown(self) -> Path:
        return self.root / "02_source/qa.md"

    @property
    def source_final(self) -> Path:
        return self.root / "02_source/final.txt"

    @property
    def glossary_review(self) -> Path:
        return self.root / "03_terminology/glossary_review.tsv"

    @property
    def termbase(self) -> Path:
        return self.root / "03_terminology/termbase.json"

    @property
    def translation_prompt(self) -> Path:
        return self.root / "04_translation/prompt.txt"

    @property
    def translation_draft(self) -> Path:
        return self.root / "04_translation/draft.txt"

    @property
    def translation_review(self) -> Path:
        return self.root / "04_translation/review.txt"

    @property
    def translation_qa_markdown(self) -> Path:
        return self.root / "04_translation/qa.md"

    @property
    def translation_revisions(self) -> Path:
        return self.root / "04_translation/revisions"

    @property
    def final_translation(self) -> Path:
        return self.root / "05_output/translation.txt"

    @property
    def segments_dir(self) -> Path:
        return self.root / ".glk/segments"

    @property
    def source_segments(self) -> Path:
        return self.segments_dir / "source.jsonl"

    @property
    def source_manifest(self) -> Path:
        return self.segments_dir / "source_manifest.json"

    @property
    def approved_source_segments(self) -> Path:
        return self.segments_dir / "approved_source.jsonl"

    @property
    def translation_segments(self) -> Path:
        return self.segments_dir / "translation.jsonl"

    @property
    def approved_translation_segments(self) -> Path:
        return self.segments_dir / "approved_translation.jsonl"

    @property
    def state_dir(self) -> Path:
        return self.root / ".glk/state"

    @property
    def pdf_acquisition_state(self) -> Path:
        return self.state_dir / "pdf_acquisition.json"

    @property
    def image_ocr_state(self) -> Path:
        return self.state_dir / "image_ocr.json"

    @property
    def segmentation_state(self) -> Path:
        return self.state_dir / "segmentation.json"

    @property
    def source_qa_state(self) -> Path:
        return self.state_dir / "source_qa.json"

    @property
    def source_review_state(self) -> Path:
        return self.state_dir / "source_review.json"

    @property
    def glossary_build_state(self) -> Path:
        return self.state_dir / "glossary_build.json"

    @property
    def glossary_import_state(self) -> Path:
        return self.state_dir / "glossary_import.json"

    @property
    def translation_state(self) -> Path:
        return self.state_dir / "translation.json"

    @property
    def translation_review_state(self) -> Path:
        return self.state_dir / "translation_review.json"

    @property
    def source_qa_json(self) -> Path:
        return self.root / ".glk/reports/source_qa.json"

    @property
    def translation_qa_json(self) -> Path:
        return self.root / ".glk/reports/translation_qa.json"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()
