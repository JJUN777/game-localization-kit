"""Deterministic checks shared by machine translation and human review."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any


_CURLY_TOKEN_PATTERN = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")
_SQUARE_TOKEN_PATTERN = re.compile(r"\[[^\]\n]+\]")
_HTML_TAG_PATTERN = re.compile(r"</?[^>\n]+>")
_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*%?")


@dataclass(frozen=True, slots=True)
class TranslationContractIssue:
    code: str
    message: str


def _contains_term(text: str, term: str) -> bool:
    clean = term.strip()
    if not clean:
        return False
    prefix = r"(?<!\w)" if clean[0].isalnum() else ""
    suffix = r"(?!\w)" if clean[-1].isalnum() else ""
    return re.search(prefix + re.escape(clean) + suffix, text, re.IGNORECASE) is not None


def _contains_target_text(text: str, expected: str) -> bool:
    return expected.strip().casefold() in text.casefold()


def _entry_variants(entry: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys([entry["source_term"], *entry.get("variants", [])])
    )


def _preserved_items(text: str) -> dict[str, Counter[str]]:
    return {
        "curly_token_changed": Counter(_CURLY_TOKEN_PATTERN.findall(text)),
        "square_token_changed": Counter(_SQUARE_TOKEN_PATTERN.findall(text)),
        "html_tag_changed": Counter(_HTML_TAG_PATTERN.findall(text)),
        "number_changed": Counter(_NUMBER_PATTERN.findall(text)),
    }


def check_translation_contract(
    *,
    source_text: str,
    translated_text: str,
    termbase_entries: list[dict[str, Any]],
) -> list[TranslationContractIssue]:
    """Return deterministic preservation and terminology violations."""
    issues: list[TranslationContractIssue] = []
    labels = {
        "curly_token_changed": "curly tokens",
        "square_token_changed": "square tokens",
        "html_tag_changed": "HTML tags",
        "number_changed": "numbers",
    }
    source_items = _preserved_items(source_text)
    target_items = _preserved_items(translated_text)
    for code, source_values in source_items.items():
        if source_values != target_items[code]:
            issues.append(
                TranslationContractIssue(
                    code=code,
                    message=f"{labels[code]} changed",
                )
            )

    for entry in termbase_entries:
        matching_variants = [
            variant
            for variant in _entry_variants(entry)
            if _contains_term(source_text, variant)
        ]
        if not matching_variants:
            continue
        if entry["status"] == "approved":
            if not _contains_target_text(translated_text, entry["translation"]):
                issues.append(
                    TranslationContractIssue(
                        code="approved_term_missing",
                        message=(
                            f"term {entry['source_term']!r} must use "
                            f"{entry['translation']!r}"
                        ),
                    )
                )
        elif not any(
            _contains_target_text(translated_text, variant)
            for variant in matching_variants
        ):
            issues.append(
                TranslationContractIssue(
                    code="keep_term_changed",
                    message=f"keep term {entry['source_term']!r} was changed",
                )
            )
    return issues
