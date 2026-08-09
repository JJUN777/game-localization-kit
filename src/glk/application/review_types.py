"""Typed browser-facing document contracts shared by review services."""

from __future__ import annotations

from typing import Any, TypedDict


class ReviewProject(TypedDict):
    id: str
    name: str
    source_language: str
    target_language: str


class SourceReviewGroup(TypedDict):
    id: str
    source_type: str
    page: int | None
    source_file: str
    label: str
    image_url: str
    layout_warnings: int


class SourceReviewBlock(TypedDict):
    id: str
    group_id: str
    source_type: str
    source_file: str
    page: int | None
    block_type: str
    text: str
    raw_text: str
    bbox: list[float] | None
    manual: bool
    excluded: bool
    changed: bool
    warnings: list[str]
    layout_warnings: int
    issues: list[dict[str, Any]]


class SourceReviewSummary(TypedDict):
    blocks: int
    included: int
    excluded: int
    manual: int
    changed: int
    warnings: int
    layout_warnings: int
    issues: int


class SourceReviewDocument(TypedDict):
    ok: bool
    project_id: str
    project_name: str
    source_type: str
    review_status: str
    review_sha256: str
    source_sha256: str | None
    groups: list[SourceReviewGroup]
    blocks: list[SourceReviewBlock]
    summary: SourceReviewSummary
    original_pdf_url: str | None


class GlossaryReviewRow(TypedDict):
    status: str
    source_term: str
    translation: str
    category: str
    note: str
    variants: str
    occurrences: str
    locations: str
    example: str
    candidate_id: str
    row_key: str
    manual: bool


class GlossaryReviewSummary(TypedDict):
    rows: int
    review: int
    approved: int
    keep: int
    rejected: int
    manual: int
    missing_translation: int


class GlossaryReviewDocument(TypedDict):
    schema_version: int
    project: ReviewProject
    review_file: str
    review_sha256: str
    statuses: list[str]
    categories: list[str]
    summary: GlossaryReviewSummary
    termbase_status: str
    rows: list[GlossaryReviewRow]


class TranslationReviewIssuePayload(TypedDict):
    severity: str
    code: str
    block_id: str | None
    message: str


class TranslationReviewTerm(TypedDict):
    source_term: str
    translation: str
    status: str
    category: str
    variants: list[str]
    note: str


class TranslationReviewBlock(TypedDict):
    id: str
    source_file: str
    page: int | None
    source_order: int
    block_type: str
    source: str
    draft_translation: str
    translation: str
    changed: bool
    issues: list[TranslationReviewIssuePayload]
    relevant_terms: list[TranslationReviewTerm]


class TranslationReviewSummary(TypedDict):
    blocks: int
    changed: int
    errors: int
    overridable_errors: int
    blocking_errors: int
    warnings: int
    info: int
    passed: bool


class TranslationReviewDocument(TypedDict):
    schema_version: int
    project: ReviewProject
    review_sha256: str
    review_status: str
    final_translation_approved: bool
    summary: TranslationReviewSummary
    general_issues: list[TranslationReviewIssuePayload]
    termbase: list[TranslationReviewTerm]
    blocks: list[TranslationReviewBlock]
