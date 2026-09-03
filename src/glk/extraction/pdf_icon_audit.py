"""Constrained visual audit for icons omitted from embedded PDF text."""

from __future__ import annotations

import json
import re
from typing import Any


PDF_ICON_AUDIT_PROMPT_VERSION = "pdf-icon-audit-v2"
PDF_ICON_AUDIT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "icons": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "marker": {"type": "string"},
                    "description": {"type": "string"},
                    "after_unit_id": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": [
                    "marker",
                    "description",
                    "after_unit_id",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["icons", "summary"],
    "additionalProperties": False,
}

_TEXT_UNIT_PATTERN = re.compile(
    r"\[(?!(?:ICON|ILLEGIBLE)\])[A-Z][A-Z0-9_]*\]"
    r"|\[ICON:\s*[^\]\n]+\]"
    r"|\{[A-Za-z][A-Za-z0-9_]*\}"
    r"|\w+(?:['’\-]\w+)*"
    r"|[^\w\s]",
    re.UNICODE | re.IGNORECASE,
)
_TOKEN_DEFINITION_PATTERN = re.compile(
    r"^\s*-\s*\[([A-Z][A-Z0-9_]*)\]\s*:\s*(\S.*?)\s*$",
    re.MULTILINE,
)
_TOKEN_MARKER_PATTERN = re.compile(r"^\[([A-Z][A-Z0-9_]*)\]$")
_UNRESOLVED_MARKER_PATTERN = re.compile(
    r"^\[ICON:\s*([^\]\n]{1,120})\]$",
    re.IGNORECASE,
)


class PdfIconAuditValidationError(ValueError):
    """Raised when a PDF icon audit response cannot be applied safely."""


def text_units(text: str) -> list[dict[str, Any]]:
    """Return stable units and offsets for deterministic marker insertion."""
    return [
        {
            "id": f"U{index:03d}",
            "text": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
        for index, match in enumerate(_TEXT_UNIT_PATTERN.finditer(text), start=1)
    ]


def icon_token_definitions(prompt: str) -> dict[str, str]:
    """Extract only explicit ``- [TOKEN]: visual description`` definitions."""
    return {
        match.group(1): match.group(2).strip()
        for match in _TOKEN_DEFINITION_PATTERN.finditer(prompt)
    }


def build_pdf_icon_audit_prompt(
    *,
    page: int,
    block_id: str,
    text: str,
    target_bbox: tuple[int, int, int, int],
    token_definitions: dict[str, str],
) -> str:
    """Build the fixed prompt used for one selected PDF source block."""
    units = [{"id": item["id"], "text": item["text"]} for item in text_units(text)]
    return f"""You inspect a cropped English board-game PDF page for meaningful icons
that are visible in a selected text block but absent from its embedded PDF text.

This is an icon audit, not OCR. Never rewrite, correct, translate, remove, or add text.
Return only insertion markers anchored after one supplied text unit.

Hard rules:
1. Inspect only the target rectangle. Surrounding crop pixels are context only.
2. Report inline icons that carry rules, cost, resource, action, or component meaning.
3. Ignore bullets, borders, arrows used only for flow, logos, and decorative artwork.
4. Use a [TOKEN] only when it is explicitly defined below and the visible icon
   confidently matches that definition.
5. Otherwise use [ICON: concise visible shape and color description]. Never infer a
   game-specific name from context alone.
6. after_unit_id must be START or one supplied unit ID. START means before all text.
7. Preserve visual left-to-right reading order when multiple icons share an anchor.
8. Return an empty icons list when no meaningful missing icon is visible.
9. Do not report an icon that is already represented by a [TOKEN] or [ICON: ...]
   marker in the current text.

Page: {page}
Block ID: {block_id}
Target rectangle in crop pixels [x0,y0,x1,y1]: {json.dumps(target_bbox)}
Exact current text: {json.dumps(text, ensure_ascii=False)}
Text units: {json.dumps(units, ensure_ascii=False, separators=(",", ":"))}
Allowed icon token definitions: {json.dumps(token_definitions, ensure_ascii=False)}
"""


def validate_pdf_icon_audit_result(
    value: Any,
    *,
    text: str,
    token_definitions: dict[str, str],
) -> dict[str, Any]:
    """Validate provider output and attach deterministic character offsets."""
    if not isinstance(value, dict):
        raise PdfIconAuditValidationError("Icon audit response must be an object.")
    icons = value.get("icons")
    summary = value.get("summary")
    if not isinstance(icons, list) or not isinstance(summary, str):
        raise PdfIconAuditValidationError(
            "Icon audit response requires icons and summary."
        )
    if len(icons) > 32 or len(summary) > 500:
        raise PdfIconAuditValidationError("Icon audit response is too large.")
    units = text_units(text)
    anchors = {"START": 0, **{item["id"]: item["end"] for item in units}}
    validated: list[dict[str, Any]] = []
    for icon in icons:
        if not isinstance(icon, dict):
            raise PdfIconAuditValidationError("Every icon result must be an object.")
        marker = icon.get("marker")
        description = icon.get("description")
        anchor = icon.get("after_unit_id")
        confidence = icon.get("confidence")
        if (
            not isinstance(marker, str)
            or not isinstance(description, str)
            or not isinstance(anchor, str)
            or not isinstance(confidence, str)
        ):
            raise PdfIconAuditValidationError("Icon result fields must be strings.")
        if not description.strip() or len(description) > 200:
            raise PdfIconAuditValidationError("Icon description is invalid.")
        if anchor not in anchors:
            raise PdfIconAuditValidationError(f"Unknown icon anchor: {anchor}")
        if confidence not in {"high", "medium", "low"}:
            raise PdfIconAuditValidationError("Icon confidence is invalid.")
        token_match = _TOKEN_MARKER_PATTERN.fullmatch(marker)
        unresolved_match = _UNRESOLVED_MARKER_PATTERN.fullmatch(marker)
        if token_match:
            if token_match.group(1) not in token_definitions:
                raise PdfIconAuditValidationError(
                    f"Icon audit returned undefined token: {marker}"
                )
        elif not unresolved_match:
            raise PdfIconAuditValidationError(f"Invalid icon marker: {marker}")
        validated.append(
            {
                "marker": marker,
                "description": description.strip(),
                "after_unit_id": anchor,
                "confidence": confidence,
                "insert_at": anchors[anchor],
            }
        )
    return {"icons": validated, "summary": summary.strip()}


def insert_icon_markers(text: str, icons: list[dict[str, Any]]) -> str:
    """Insert validated markers without allowing provider-authored text changes."""
    grouped: dict[int, list[str]] = {}
    for icon in icons:
        position = icon.get("insert_at")
        marker = icon.get("marker")
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or not 0 <= position <= len(text)
            or not isinstance(marker, str)
        ):
            raise PdfIconAuditValidationError("Icon insertion is invalid.")
        grouped.setdefault(position, []).append(marker)
    result = text
    for position in sorted(grouped, reverse=True):
        markers = " ".join(grouped[position])
        prefix = " " if position > 0 and not result[position - 1].isspace() else ""
        suffix = " " if position < len(result) and not result[position].isspace() else ""
        result = result[:position] + prefix + markers + suffix + result[position:]
    return result
