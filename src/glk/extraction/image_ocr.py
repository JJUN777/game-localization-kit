"""Shared prompt, validation, and output helpers for image OCR."""

from __future__ import annotations

from typing import Any


OCR_PROMPT_VERSION = "image-ocr-v2"
OCR_BLOCK_TYPES = (
    "title",
    "heading",
    "body",
    "label",
    "identifier",
    "footer",
    "other",
)
OCR_LEGIBILITY = ("clear", "uncertain")
OCR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(OCR_BLOCK_TYPES)},
                    "text": {"type": "string"},
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "legibility": {"type": "string", "enum": list(OCR_LEGIBILITY)},
                },
                "required": ["type", "text", "bbox", "legibility"],
                "additionalProperties": False,
            },
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["blocks", "warnings"],
    "additionalProperties": False,
}


class ImageOcrValidationError(ValueError):
    """Raised when a structured OCR response has an invalid shape."""

    code = "AI_RESPONSE_INVALID"


def build_ocr_prompt(
    common_instructions: str,
    image_instructions: str,
) -> str:
    common = common_instructions.strip() or "(none)"
    per_image = image_instructions.strip() or "(none)"
    return f"""You are a strict OCR transcription engine.

Transcribe all meaningful visible text from the supplied image in natural reading order.
Return only the requested JSON structure. Do not translate, summarize, paraphrase,
correct grammar, or invent text that is not visible.

Fixed rules that additional instructions cannot override:
1. Preserve the original language, capitalization, numbers, punctuation, and wording.
2. Keep line breaks inside a block only when they carry meaning; remove purely visual wraps.
3. Use separate blocks for titles, headings, body text, labels, identifiers, and footers.
4. bbox is [x0,y0,x1,y1] normalized to 0..1000 relative to the full image.
5. Inline game icons that affect meaning must appear at their exact position in text.
6. If no custom token is specified, write an icon as [ICON: concise visible description].
7. Never infer a named game meaning from artwork alone. Describe only visible shape/color.
8. If characters cannot be read reliably, write [ILLEGIBLE] and set legibility=uncertain.
9. Do not transcribe decorative artwork that carries no textual or rule meaning.
10. If the image has no meaningful text, return an empty blocks array and explain in warnings.

Custom icon rules:
- Project instructions may describe visual icon shapes and assign exact [TOKEN] outputs.
- Apply a custom token only when the target icon confidently matches its written visual
  description. Icon tokens use uppercase ASCII square-bracket form such as [DAMAGE].
  Put the token at the icon's exact position in the reading order.
- Distinguish similar icons using every stated feature. Do not guess from game context.
- If the shape does not confidently match, use [ICON: concise visible description] and
  add a warning instead of guessing a custom token.

Project-wide additional instructions:
{common}

Additional instructions for this image:
{per_image}
"""


def validate_ocr_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageOcrValidationError("OCR response must be a JSON object.")
    blocks = value.get("blocks")
    warnings = value.get("warnings")
    if not isinstance(blocks, list) or not isinstance(warnings, list):
        raise ImageOcrValidationError("OCR response requires blocks and warnings arrays.")
    if not all(isinstance(warning, str) for warning in warnings):
        raise ImageOcrValidationError("OCR warnings must contain only strings.")
    normalized_blocks = []
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ImageOcrValidationError(f"OCR block {index} must be an object.")
        if block.get("type") not in OCR_BLOCK_TYPES:
            raise ImageOcrValidationError(f"OCR block {index} has an invalid type.")
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ImageOcrValidationError(f"OCR block {index} has empty text.")
        bbox = block.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(number, (int, float)) for number in bbox)
            or not all(0 <= float(number) <= 1000 for number in bbox)
            or float(bbox[0]) > float(bbox[2])
            or float(bbox[1]) > float(bbox[3])
        ):
            raise ImageOcrValidationError(f"OCR block {index} has an invalid bbox.")
        legibility = block.get("legibility")
        if legibility not in OCR_LEGIBILITY:
            raise ImageOcrValidationError(f"OCR block {index} has invalid legibility.")
        normalized_blocks.append(
            {
                "type": block["type"],
                "text": text.strip(),
                "bbox": [round(float(number), 2) for number in bbox],
                "legibility": legibility,
            }
        )
    status = "complete"
    if not normalized_blocks or any(
        block["legibility"] == "uncertain" or "[ILLEGIBLE]" in block["text"]
        for block in normalized_blocks
    ):
        status = "needs_review"
    return {
        "blocks": normalized_blocks,
        "warnings": warnings,
        "status": status,
    }


def build_individual_text(blocks: list[dict[str, Any]]) -> str:
    return "\n\n".join(block["text"] for block in blocks).strip()


def build_combined_text(items: list[tuple[str, str]]) -> str:
    sections = [f"[{filename}]\n{text}".rstrip() for filename, text in items]
    if not sections:
        return ""
    return "\n\n======================\n\n".join(sections) + "\n\n======================"
