"""PDF layout reconstruction proof of concept.

The model never rewrites source text. It only orders and groups fragment IDs,
then this script rebuilds the page from the original PDF fragments.
"""

from __future__ import annotations

import argparse
from collections import Counter
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

import pymupdf
from google.genai import types
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(LEGACY_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LEGACY_SCRIPTS))

from common import (  # noqa: E402
    call_gemini_with_retry,
    init_config_only,
    init_pipeline,
)


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


def parse_page_selection(value: str | None, page_count: int) -> list[int]:
    """Parse a 1-based page expression such as '1,3-5'."""
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
            selected.update(range(start - 1, end))
        else:
            selected.add(int(item) - 1)

    invalid = sorted(page + 1 for page in selected if page < 0 or page >= page_count)
    if invalid:
        raise ValueError(f"Page numbers out of range: {invalid} (document has {page_count} pages)")
    return sorted(selected)


def _round_bbox(bbox: list[float] | tuple[float, ...]) -> list[float]:
    return [round(float(value), 2) for value in bbox]


def extract_line_fragments(page: pymupdf.Page, page_number: int) -> list[dict[str, Any]]:
    """Extract geometry-aware text lines without deciding their reading order."""
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

            fragment_id = f"P{page_number:03d}-F{len(fragments) + 1:03d}"
            fragments.append(
                {
                    "id": fragment_id,
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
        pil_image = image.convert("RGB").copy()
    return png_bytes, pil_image


def build_layout_prompt(page_number: int, fragments: list[dict[str, Any]]) -> str:
    compact_fragments = [
        {
            "id": fragment["id"],
            "text": fragment["text"],
            "bbox": fragment["bbox"],
        }
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
   the entire left panel (its heading and all body content) before starting the next
   panel. Never place sibling panel headings together ahead of their own body text.
5. Group visual line wraps belonging to the same paragraph or list item into one block.
6. Keep different headings, paragraphs, bullets, captions, and labels in separate blocks.
7. Full-width regions may appear before, between, or after column regions.
8. Set include_in_text=false only for page numbers or obvious decorative extraction
   artifacts. Component names, captions, diagram labels, and rules text must remain true.
9. Use artifact only for corrupted or meaningless glyph fragments, not for text merely
   because it appears inside a diagram.
10. Explain exclusions briefly in reason. Use an empty reason for included blocks.

PDF page index: {page_number}
Fragments:
{fragment_json}
"""


def request_layout(client, model_name: str, prompt: str, page_image: Image.Image) -> dict[str, Any]:
    generation_config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_json_schema=RESPONSE_SCHEMA,
    )
    response = call_gemini_with_retry(
        client,
        model_name,
        [prompt, page_image],
        generation_config=generation_config,
    )
    if not response.text:
        raise ValueError("Gemini returned an empty layout response")
    result = json.loads(response.text)
    if not isinstance(result, dict) or not isinstance(result.get("blocks"), list):
        raise ValueError("Gemini layout response has an invalid structure")
    return result


def validate_layout(fragments: list[dict[str, Any]], layout: dict[str, Any]) -> dict[str, Any]:
    expected_ids = [fragment["id"] for fragment in fragments]
    returned_ids = [
        fragment_id
        for block in layout.get("blocks", [])
        for fragment_id in block.get("fragment_ids", [])
    ]
    counts = Counter(returned_ids)
    expected_set = set(expected_ids)
    returned_set = set(returned_ids)

    missing = sorted(expected_set - returned_set)
    unknown = sorted(returned_set - expected_set)
    duplicates = sorted(fragment_id for fragment_id, count in counts.items() if count > 1)
    valid = not missing and not unknown and not duplicates and len(returned_ids) == len(expected_ids)
    report = {
        "valid": valid,
        "expected_count": len(expected_ids),
        "returned_count": len(returned_ids),
        "missing": missing,
        "unknown": unknown,
        "duplicates": duplicates,
    }
    if not valid:
        raise ValueError(f"Layout response failed fragment validation: {report}")
    return report


def join_fragment_texts(texts: list[str]) -> str:
    """Remove visual line wraps while preserving the extracted words."""
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
    reconstructed = []
    for block in layout["blocks"]:
        fragment_ids = block["fragment_ids"]
        texts = [by_id[fragment_id]["text"] for fragment_id in fragment_ids]
        reconstructed.append(
            {
                **block,
                "text": join_fragment_texts(texts),
            }
        )
    return reconstructed


def _union_bbox(items: list[dict[str, Any]]) -> list[float]:
    return [
        min(item["bbox"][0] for item in items),
        min(item["bbox"][1] for item in items),
        max(item["bbox"][2] for item in items),
        max(item["bbox"][3] for item in items),
    ]


def group_native_text_blocks(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore PyMuPDF's native text blocks before applying local ordering rules."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for fragment in fragments:
        grouped.setdefault(fragment["block_index"], []).append(fragment)

    blocks = []
    for block_index, block_fragments in grouped.items():
        block_fragments.sort(key=lambda item: (item["line_index"], item["bbox"][1]))
        blocks.append(
            {
                "native_block_index": block_index,
                "fragment_ids": [item["id"] for item in block_fragments],
                "text": join_fragment_texts([item["text"] for item in block_fragments]),
                "bbox": _union_bbox(block_fragments),
                "max_font_size": max(
                    (size for item in block_fragments for size in item["font_sizes"]),
                    default=0,
                ),
            }
        )
    return blocks


def _axis_gaps(
    blocks: list[dict[str, Any]], axis: int, minimum_gap: float
) -> list[tuple[float, float]]:
    """Return whitespace gaps as (gap size, cut coordinate) for one axis."""
    start_index, end_index = (0, 2) if axis == 0 else (1, 3)
    intervals = sorted(
        (block["bbox"][start_index], block["bbox"][end_index]) for block in blocks
    )
    if len(intervals) < 2:
        return []

    gaps: list[tuple[float, float]] = []
    current_end = intervals[0][1]
    for start, end in intervals[1:]:
        gap = start - current_end
        if gap >= minimum_gap:
            gaps.append((gap, current_end + gap / 2))
        current_end = max(current_end, end)
    return gaps


def local_xy_cut_order(
    blocks: list[dict[str, Any]], page_width: float, page_height: float
) -> list[dict[str, Any]]:
    """Order blocks offline by recursively cutting the largest empty band."""
    if len(blocks) <= 1:
        return blocks

    candidates: list[tuple[float, int, float]] = []
    for axis, dimension in ((0, page_width), (1, page_height)):
        for gap, cut in _axis_gaps(blocks, axis, minimum_gap=6.0):
            candidates.append((gap / max(dimension, 1.0), axis, cut))

    for _, axis, cut in sorted(candidates, reverse=True):
        end_index = 2 if axis == 0 else 3
        start_index = 0 if axis == 0 else 1
        first = [block for block in blocks if block["bbox"][end_index] < cut]
        second = [block for block in blocks if block["bbox"][start_index] > cut]
        if len(first) + len(second) != len(blocks) or not first or not second:
            continue
        return local_xy_cut_order(first, page_width, page_height) + local_xy_cut_order(
            second, page_width, page_height
        )

    return sorted(blocks, key=lambda block: (block["bbox"][1], block["bbox"][0]))


def classify_local_block(block: dict[str, Any], page_height: float) -> tuple[str, bool]:
    text = block["text"].strip()
    bbox = block["bbox"]
    if re.fullmatch(r"\d{1,4}", text) and bbox[1] >= page_height * 0.9:
        return "page_number", False
    if re.match(r"^(?:[•●▪◦]|\d+[a-z]?\.)\s*", text, re.IGNORECASE):
        return "list_item", True
    letters = "".join(character for character in text if character.isalpha())
    if letters and letters.isupper() and len(text) <= 80:
        return "heading", True
    if block["max_font_size"] >= 15 and len(text) <= 100:
        return "heading", True
    if len(text) <= 60:
        return "label", True
    return "paragraph", True


def merge_local_continuations(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a paragraph that continues at the top of the next column."""
    merged: list[dict[str, Any]] = []
    for block in blocks:
        if (
            merged
            and merged[-1]["type"] == "paragraph"
            and block["type"] == "paragraph"
            and not re.search(r"[.!?][\"'’”)]?$", merged[-1]["text"])
            and re.match(r"^[a-z]", block["text"])
        ):
            previous = merged[-1]
            previous["fragment_ids"].extend(block["fragment_ids"])
            previous["text"] = join_fragment_texts([previous["text"], block["text"]])
            previous["bbox"] = _union_bbox([previous, block])
            previous["reason"] = "local continuation heuristic"
        else:
            merged.append(block)
    return merged


def reconstruct_local_layout(
    fragments: list[dict[str, Any]], page_width: float, page_height: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a deterministic, no-network layout baseline."""
    native_blocks = group_native_text_blocks(fragments)
    ordered = local_xy_cut_order(native_blocks, page_width, page_height)
    reconstructed = []
    for block in ordered:
        block_type, include_in_text = classify_local_block(block, page_height)
        reconstructed.append(
            {
                "type": block_type,
                "fragment_ids": block["fragment_ids"],
                "include_in_text": include_in_text,
                "reason": "" if include_in_text else "local page-number heuristic",
                "text": block["text"],
                "bbox": block["bbox"],
                "native_block_index": block["native_block_index"],
            }
        )
    reconstructed = merge_local_continuations(reconstructed)
    layout = {
        "method": "local_recursive_xy_cut",
        "blocks": [
            {
                "type": block["type"],
                "fragment_ids": block["fragment_ids"],
                "include_in_text": block["include_in_text"],
                "reason": block["reason"],
            }
            for block in reconstructed
        ],
    }
    return layout, reconstructed


def build_page_text(blocks: list[dict[str, Any]]) -> str:
    included = [block["text"] for block in blocks if block["include_in_text"] and block["text"]]
    return "\n\n".join(included).strip()


def write_json(path: Path, value: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def write_text(path: Path, value: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write(value)
        if value and not value.endswith("\n"):
            file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def resolve_input_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    return path


def run(args: argparse.Namespace) -> int:
    input_path = resolve_input_path(args.file)
    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = (PROJECT_ROOT / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    model_name = None
    client = None
    if not args.extract_only and not args.local_only:
        config = init_config_only()
        if not config:
            return 2
        model_name = args.model or config.get("translation", {}).get("model_name")
        if not model_name:
            logging.error("No model name configured")
            return 2
        _, client = init_pipeline()
        if client is None:
            return 2

    document = pymupdf.open(input_path)
    try:
        page_indexes = parse_page_selection(args.pages, document.page_count)
        logging.info(
            "PoC input: %s (%d pages); selected pages: %s",
            input_path,
            document.page_count,
            [page + 1 for page in page_indexes],
        )

        model_pages: dict[int, str] = {}
        local_pages: dict[int, str] = {}
        failures: list[dict[str, Any]] = []
        for page_index in page_indexes:
            page_number = page_index + 1
            page = document[page_index]
            fragments = extract_line_fragments(page, page_number)
            png_bytes, page_image = render_page(page, args.scale)
            page_stem = f"page_{page_number:03d}"
            image_path = output_dir / f"{page_stem}.png"
            fragment_path = output_dir / f"{page_stem}_fragments.json"
            result_path = output_dir / f"{page_stem}_result.json"
            text_path = output_dir / f"{page_stem}_reconstructed.txt"

            image_path.write_bytes(png_bytes)
            write_json(
                fragment_path,
                {
                    "source_pdf": str(input_path),
                    "page": page_number,
                    "page_size": [round(page.rect.width, 2), round(page.rect.height, 2)],
                    "fragments": fragments,
                },
            )
            logging.info("Page %d: extracted %d fragments", page_number, len(fragments))

            if args.extract_only:
                continue

            local_layout, local_blocks = reconstruct_local_layout(
                fragments, page.rect.width, page.rect.height
            )
            local_validation = validate_layout(fragments, local_layout)
            local_text = build_page_text(local_blocks)
            write_json(
                output_dir / f"{page_stem}_local_result.json",
                {
                    "source_pdf": str(input_path),
                    "page": page_number,
                    "method": local_layout["method"],
                    "validation": local_validation,
                    "fragments": fragments,
                    "layout": local_layout,
                    "reconstructed_blocks": local_blocks,
                },
            )
            write_text(output_dir / f"{page_stem}_local.txt", local_text)
            local_pages[page_number] = local_text
            logging.info("Page %d: local layout baseline completed", page_number)

            if args.local_only:
                continue
            if result_path.exists() and text_path.exists() and not args.force:
                logging.info("Page %d: reusing existing PoC result", page_number)
                model_pages[page_number] = text_path.read_text(encoding="utf-8").strip()
                continue

            try:
                prompt = build_layout_prompt(page_number, fragments)
                layout = request_layout(client, model_name, prompt, page_image)
                validation = validate_layout(fragments, layout)
                blocks = reconstruct_blocks(fragments, layout)
                page_text = build_page_text(blocks)
                write_json(
                    result_path,
                    {
                        "source_pdf": str(input_path),
                        "page": page_number,
                        "model": model_name,
                        "validation": validation,
                        "fragments": fragments,
                        "layout": layout,
                        "reconstructed_blocks": blocks,
                    },
                )
                write_text(text_path, page_text)
                model_pages[page_number] = page_text
                logging.info("Page %d: layout reconstruction completed", page_number)
            except Exception as error:
                logging.exception("Page %d failed: %s", page_number, error)
                failures.append({"page": page_number, "error": str(error)})

        if local_pages:
            local_combined = "\n\n".join(
                f"[PAGE {page_number}]\n{local_pages[page_number]}"
                for page_number in sorted(local_pages)
            )
            write_text(output_dir / "local_reconstructed.txt", local_combined)
            write_json(
                output_dir / "local_run_summary.json",
                {
                    "source_pdf": str(input_path),
                    "method": "local_recursive_xy_cut",
                    "selected_pages": [page + 1 for page in page_indexes],
                    "successful_pages": sorted(local_pages),
                },
            )

        if not args.extract_only and not args.local_only:
            combined = "\n\n".join(
                f"[PAGE {page_number}]\n{model_pages[page_number]}"
                for page_number in sorted(model_pages)
            )
            write_text(output_dir / "reconstructed.txt", combined)
            write_json(
                output_dir / "run_summary.json",
                {
                    "source_pdf": str(input_path),
                    "model": model_name,
                    "selected_pages": [page + 1 for page in page_indexes],
                    "successful_pages": sorted(model_pages),
                    "failures": failures,
                },
            )

        return 1 if failures else 0
    finally:
        document.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PoC: reconstruct PDF reading order and paragraphs using fragment IDs."
    )
    parser.add_argument("--file", required=True, help="Input PDF path")
    parser.add_argument("--pages", help="1-based pages, e.g. 1,3-4 (default: all)")
    parser.add_argument("--output-dir", default="97_layout_poc", help="PoC output directory")
    parser.add_argument("--model", help="Override the configured Gemini model")
    parser.add_argument("--scale", type=float, default=1.5, help="Page rendering scale")
    parser.add_argument("--extract-only", action="store_true", help="Skip Gemini and write fragments only")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Reconstruct with deterministic local XY-cut rules; do not call Gemini",
    )
    parser.add_argument("--force", action="store_true", help="Regenerate existing model results")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
