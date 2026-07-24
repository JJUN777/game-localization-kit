"""Read and safely update the human-editable glossary review TSV."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from glk.application._hashing import sha256_bytes as _sha256_bytes
from glk.application._io import write_bytes_atomic as _write_bytes_atomic
from glk.application.glossary_service import GLOSSARY_REVIEW_COLUMNS
from glk.application.project_service import inspect_project, load_project
from glk.domain.workspace import WorkspacePaths


GLOSSARY_REVIEW_STATUSES = ("review", "approved", "keep", "rejected")
GLOSSARY_REVIEW_CATEGORIES = (
    "term",
    "proper_noun",
    "ability",
    "component",
    "ui",
    "phrase",
)


class GlossaryReviewError(ValueError):
    """Raised when the browser glossary review cannot be processed safely."""


def _parse_tsv(data: bytes) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise GlossaryReviewError("Glossary review TSV must be UTF-8.") from error
    try:
        records = list(
            csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
        )
    except csv.Error as error:
        raise GlossaryReviewError(f"Invalid glossary review TSV: {error}") from error
    if not records:
        raise GlossaryReviewError("Glossary review TSV is empty.")
    if tuple(records[0]) != GLOSSARY_REVIEW_COLUMNS:
        raise GlossaryReviewError(
            "Glossary review TSV columns must exactly match: "
            + ", ".join(GLOSSARY_REVIEW_COLUMNS)
        )

    rows: list[dict[str, str]] = []
    for record_number, record in enumerate(records[1:], start=2):
        if not record or not any(value.strip() for value in record):
            continue
        if len(record) != len(GLOSSARY_REVIEW_COLUMNS):
            raise GlossaryReviewError(
                f"Glossary review TSV record {record_number} has {len(record)} fields; "
                f"expected {len(GLOSSARY_REVIEW_COLUMNS)}."
            )
        rows.append(dict(zip(GLOSSARY_REVIEW_COLUMNS, record)))
    return rows


def _render_tsv(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=GLOSSARY_REVIEW_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _clean_single_line(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise GlossaryReviewError(f"{field} must be a string.")
    return " ".join(value.split())


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise GlossaryReviewError(f"{field} must be a string.")
    return value.strip()


def _summarize(rows: list[dict[str, str]]) -> dict[str, int]:
    summary = {
        "rows": len(rows),
        "review": 0,
        "approved": 0,
        "keep": 0,
        "rejected": 0,
        "manual": 0,
        "missing_translation": 0,
    }
    for row in rows:
        status = row["status"].strip()
        if status in GLOSSARY_REVIEW_STATUSES:
            summary[status] += 1
        if not row["candidate_id"].strip() or row["candidate_id"].startswith("manual-"):
            summary["manual"] += 1
        if status == "approved" and not row["translation"].strip():
            summary["missing_translation"] += 1
    return summary


def get_project_glossary_review_document(
    *,
    project: str | Path,
    workspace_root: str | Path = "workspaces",
) -> dict[str, Any]:
    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if pipeline["glossary_status"] != "current":
        raise GlossaryReviewError(
            "Current glossary candidates were not found. Run glk glossary build first."
        )
    if not paths.glossary_review.is_file():
        raise GlossaryReviewError(
            f"Glossary review TSV not found: {paths.glossary_review}"
        )
    data = paths.glossary_review.read_bytes()
    rows = _parse_tsv(data)
    document_rows = []
    for index, row in enumerate(rows):
        value = dict(row)
        value["row_key"] = row["candidate_id"] or f"manual-new-{index}"
        value["manual"] = (
            not row["candidate_id"] or row["candidate_id"].startswith("manual-")
        )
        document_rows.append(value)
    return {
        "schema_version": 1,
        "project": {
            "id": location.manifest.project_id,
            "name": location.manifest.name,
            "source_language": location.manifest.source_language,
            "target_language": location.manifest.target_language,
        },
        "review_file": paths.relative(paths.glossary_review),
        "review_sha256": _sha256_bytes(data),
        "statuses": list(GLOSSARY_REVIEW_STATUSES),
        "categories": list(GLOSSARY_REVIEW_CATEGORIES),
        "summary": _summarize(rows),
        "termbase_status": pipeline["termbase_status"],
        "rows": document_rows,
    }


def save_project_glossary_review(
    *,
    project: str | Path,
    rows: list[dict[str, Any]],
    expected_review_sha256: str,
    workspace_root: str | Path = "workspaces",
) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise GlossaryReviewError("rows must be a list.")
    if not isinstance(expected_review_sha256, str) or not expected_review_sha256:
        raise GlossaryReviewError("review_sha256 is required.")

    location = load_project(project, workspace_root)
    paths = WorkspacePaths(location.path)
    pipeline = inspect_project(location.path)["pipeline"]
    if pipeline["glossary_status"] != "current":
        raise GlossaryReviewError(
            "Glossary review is stale. Rebuild or resolve the candidate TSV first."
        )
    if not paths.glossary_review.is_file():
        raise GlossaryReviewError("Glossary review TSV was not found.")

    current_data = paths.glossary_review.read_bytes()
    current_hash = _sha256_bytes(current_data)
    if current_hash != expected_review_sha256:
        raise GlossaryReviewError(
            "Glossary review changed after this page was loaded. Reload before saving."
        )
    current_rows = _parse_tsv(current_data)
    automatic_by_id = {
        row["candidate_id"]: row
        for row in current_rows
        if row["candidate_id"] and not row["candidate_id"].startswith("manual-")
    }

    normalized_rows: list[dict[str, str]] = []
    seen_automatic_ids: set[str] = set()
    for index, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, dict):
            raise GlossaryReviewError(f"Row {index} must be an object.")
        candidate_id = _clean_single_line(
            raw_row.get("candidate_id", ""), f"Row {index} candidate_id"
        )
        status = _clean_single_line(raw_row.get("status"), f"Row {index} status")
        category = _clean_single_line(raw_row.get("category"), f"Row {index} category")
        translation = _clean_text(
            raw_row.get("translation", ""), f"Row {index} translation"
        )
        note = _clean_text(raw_row.get("note", ""), f"Row {index} note")

        if status not in GLOSSARY_REVIEW_STATUSES:
            raise GlossaryReviewError(
                f"Row {index} has invalid status {status!r}."
            )
        if category not in GLOSSARY_REVIEW_CATEGORIES:
            raise GlossaryReviewError(
                f"Row {index} has invalid category {category!r}."
            )

        if candidate_id in automatic_by_id:
            if candidate_id in seen_automatic_ids:
                raise GlossaryReviewError(
                    f"Row {index} duplicates generated candidate_id {candidate_id!r}."
                )
            seen_automatic_ids.add(candidate_id)
            original = automatic_by_id[candidate_id]
            normalized = {
                **original,
                "status": status,
                "translation": translation,
                "category": category,
                "note": note,
            }
        else:
            if candidate_id and not candidate_id.startswith("manual-"):
                raise GlossaryReviewError(
                    f"Row {index} has unknown candidate_id {candidate_id!r}."
                )
            source_term = _clean_single_line(
                raw_row.get("source_term", ""), f"Row {index} source_term"
            )
            if not source_term:
                raise GlossaryReviewError(f"Row {index} has an empty source term.")
            original = next(
                (
                    row
                    for row in current_rows
                    if candidate_id
                    and row["candidate_id"] == candidate_id
                ),
                None,
            )
            if original and source_term != _clean_single_line(
                original["source_term"], "source_term"
            ):
                candidate_id = ""
                original = None
            normalized = {
                "status": status,
                "source_term": source_term,
                "translation": translation,
                "category": category,
                "note": note,
                "variants": original["variants"] if original else "",
                "occurrences": original["occurrences"] if original else "",
                "locations": original["locations"] if original else "",
                "example": original["example"] if original else "",
                "candidate_id": candidate_id,
            }
        if normalized["status"] == "keep":
            normalized["translation"] = normalized["source_term"]
        elif normalized["status"] == "rejected":
            normalized["translation"] = ""
        normalized_rows.append(normalized)

    missing_ids = sorted(set(automatic_by_id) - seen_automatic_ids)
    if missing_ids:
        preview = ", ".join(missing_ids[:5])
        suffix = "..." if len(missing_ids) > 5 else ""
        raise GlossaryReviewError(
            "Generated candidates cannot be deleted. Mark them rejected instead: "
            f"{preview}{suffix}"
        )

    output_data = _render_tsv(normalized_rows)
    _write_bytes_atomic(paths.glossary_review, output_data)
    return get_project_glossary_review_document(
        project=location.path,
        workspace_root=workspace_root,
    )
