"""PDF fragment extraction, layout validation, and text reconstruction."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
import json
import re
from typing import Any, Protocol

import pymupdf
from PIL import Image


PROMPT_VERSION = "layout-fragment-v1"
POSTPROCESS_VERSION = "column-continuation-v1"
BLOCK_TYPES = (
    "heading",
    "paragraph",
    "list_item",
    "caption",
    "label",
    "page_number",
    "artifact",
)
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(BLOCK_TYPES)},
                    "fragment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "include_in_text": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["type", "fragment_ids", "include_in_text", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["blocks"],
    "additionalProperties": False,
}


class LayoutValidationError(ValueError):
    """Raised when a layout response loses or invents source fragments."""


class LayoutProvider(Protocol):
    model_name: str
    prompt_version: str

    def reconstruct(
        self, page_number: int, fragments: list[dict[str, Any]], page_image: Image.Image
    ) -> dict[str, Any]: ...


def parse_page_selection(value: str | None, page_count: int) -> list[int]:
    """Parse a 1-based page expression such as '1,3-5'."""
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 1
    ):
        raise ValueError("Document page count must be a positive integer.")
    if not value:
        return list(range(page_count))
    selected: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_raw, end_raw = item.split("-", 1)
            start, end = int(start_raw), int(end_raw)
            if start > end:
                raise ValueError(f"Invalid page range: {item}")
            if start < 1 or end > page_count:
                raise ValueError(
                    f"Page range out of range: {item} "
                    f"(document has {page_count} pages)"
                )
            selected.update(range(start - 1, end))
        else:
            page = int(item)
            if page < 1 or page > page_count:
                raise ValueError(
                    f"Page number out of range: {page} "
                    f"(document has {page_count} pages)"
                )
            selected.add(page - 1)
    if not selected:
        raise ValueError("Page selection is empty.")
    return sorted(selected)


def _round_bbox(bbox: list[float] | tuple[float, ...]) -> list[float]:
    return [round(float(value), 2) for value in bbox]


def extract_line_fragments(page: pymupdf.Page, page_number: int) -> list[dict[str, Any]]:
    """Extract geometry-aware text lines without deciding reading order."""
    page_dict = page.get_text("dict", sort=False)
    fragments: list[dict[str, Any]] = []
    for block_index, block in enumerate(page_dict.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = line.get("spans", [])
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            fragments.append(
                {
                    "id": f"P{page_number:03d}-F{len(fragments) + 1:03d}",
                    "text": text,
                    "bbox": _round_bbox(line.get("bbox", (0, 0, 0, 0))),
                    "block_index": block_index,
                    "line_index": line_index,
                    "direction": [round(float(v), 3) for v in line.get("dir", (1, 0))],
                    "font_sizes": sorted(
                        {round(float(span.get("size", 0)), 2) for span in spans}
                    ),
                }
            )
    return fragments


def render_page(page: pymupdf.Page, scale: float) -> tuple[bytes, Image.Image]:
    """Render one PDF page to PNG bytes and a detached Pillow image."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    png_bytes = pixmap.tobytes("png")
    with Image.open(BytesIO(png_bytes)) as image:
        page_image = image.convert("RGB").copy()
    return png_bytes, page_image


def build_layout_prompt(page_number: int, fragments: list[dict[str, Any]]) -> str:
    compact_fragments = [
        {"id": fragment["id"], "text": fragment["text"], "bbox": fragment["bbox"]}
        for fragment in fragments
    ]
    fragment_json = json.dumps(compact_fragments, ensure_ascii=False, separators=(",", ":"))
    return f"""You reconstruct reading order for one English board-game rulebook page.

The page image is the visual source of truth for layout. The JSON list contains text
fragments extracted directly from PDF text objects. Coordinates are [x0,y0,x1,y1]
in PDF page space, where smaller y is visually higher on the page.

Your only task is to return block structure using fragment IDs.

Rules:
1. Include every supplied fragment ID exactly once. Never omit, duplicate, or invent an ID.
2. Never output or rewrite fragment text.
3. Order blocks in natural reading order. For columns, finish the left column before
   moving to the next column unless the visual design clearly indicates another flow.
   Do not use row-major ordering across columns.
4. Treat bordered cards, boxes, and side-by-side panels as independent regions. Finish
   the entire left panel before starting the next panel.
5. Group visual line wraps belonging to the same paragraph or list item into one block.
6. Keep different headings, paragraphs, bullets, captions, and labels in separate blocks.
7. Full-width regions may appear before, between, or after column regions.
8. Set include_in_text=false only for page numbers or obvious decorative extraction
   artifacts. Component names, captions, diagram labels, and rules text must remain true.
9. Use artifact only for corrupted or meaningless glyph fragments.
10. Explain exclusions briefly in reason. Use an empty reason for included blocks.

PDF page index: {page_number}
Fragments:
{fragment_json}
"""


def validate_layout(
    fragments: list[dict[str, Any]], layout: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(layout, dict) or not isinstance(layout.get("blocks"), list):
        raise LayoutValidationError("Layout response must contain a blocks array.")
    expected_ids = [fragment["id"] for fragment in fragments]
    returned_ids: list[str] = []
    for block in layout["blocks"]:
        if not isinstance(block, dict) or not isinstance(block.get("fragment_ids"), list):
            raise LayoutValidationError("Every layout block must contain fragment_ids.")
        if block.get("type") not in BLOCK_TYPES:
            raise LayoutValidationError("Every layout block must contain a supported type.")
        if not isinstance(block.get("include_in_text"), bool):
            raise LayoutValidationError("Every layout block must contain include_in_text.")
        if not isinstance(block.get("reason"), str):
            raise LayoutValidationError("Every layout block must contain a reason string.")
        if not all(isinstance(fragment_id, str) for fragment_id in block["fragment_ids"]):
            raise LayoutValidationError("fragment_ids must contain only strings.")
        returned_ids.extend(block["fragment_ids"])
    counts = Counter(returned_ids)
    expected_set = set(expected_ids)
    returned_set = set(returned_ids)
    report = {
        "valid": False,
        "expected_count": len(expected_ids),
        "returned_count": len(returned_ids),
        "missing": sorted(expected_set - returned_set),
        "unknown": sorted(returned_set - expected_set),
        "duplicates": sorted(key for key, count in counts.items() if count > 1),
    }
    report["valid"] = (
        not report["missing"]
        and not report["unknown"]
        and not report["duplicates"]
        and len(returned_ids) == len(expected_ids)
    )
    if not report["valid"]:
        raise LayoutValidationError(f"Layout failed fragment validation: {report}")
    return report


def join_fragment_texts(texts: list[str]) -> str:
    """Remove visual line wraps while preserving extracted words."""
    if not texts:
        return ""
    result = texts[0].strip()
    for raw_text in texts[1:]:
        next_text = raw_text.strip()
        if not next_text:
            continue
        if result.endswith("-") and next_text[:1].islower():
            result = result[:-1] + next_text
        elif result.endswith(("/", "(", "[", "{", "‘", "“")):
            result += next_text
        else:
            result += " " + next_text
    return " ".join(result.split())


def reconstruct_blocks(
    fragments: list[dict[str, Any]], layout: dict[str, Any]
) -> list[dict[str, Any]]:
    by_id = {fragment["id"]: fragment for fragment in fragments}
    blocks = []
    for block in layout["blocks"]:
        fragment_ids = block["fragment_ids"]
        blocks.append(
            {
                **block,
                "text": join_fragment_texts(
                    [by_id[fragment_id]["text"] for fragment_id in fragment_ids]
                ),
            }
        )
    return blocks


def merge_paragraph_continuations(
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join a paragraph split only because it crosses a column boundary."""
    merged: list[dict[str, Any]] = []
    for block in blocks:
        previous = merged[-1] if merged else None
        next_start = block.get("text", "").lstrip("\"'‘’“”(")
        should_merge = (
            previous is not None
            and previous.get("type") == "paragraph"
            and block.get("type") == "paragraph"
            and previous.get("include_in_text") is True
            and block.get("include_in_text") is True
            and not re.search(r"[.!?:;][\"'’”)]?$", previous.get("text", ""))
            and bool(next_start)
            and next_start[0].islower()
        )
        if not should_merge:
            merged.append({**block})
            continue
        previous["fragment_ids"] = [
            *previous["fragment_ids"],
            *block["fragment_ids"],
        ]
        previous["text"] = join_fragment_texts([previous["text"], block["text"]])
        previous["postprocess"] = "column-boundary continuation merged"
    return merged


def build_page_text(blocks: list[dict[str, Any]]) -> str:
    included = [
        block["text"]
        for block in blocks
        if block.get("include_in_text") and block.get("text")
    ]
    return "\n\n".join(included).strip()
