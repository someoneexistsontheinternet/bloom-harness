"""One document per captured page at every model boundary.

Capture manifests are immutable evidence, not prompts. Legacy manifests contain
overlapping layout tables and complete DOM link inventories. Keep those on disk;
expose the document and retrieval/coverage metadata only.
"""

from __future__ import annotations


PAGE_FIELDS = (
    "id",
    "rank",
    "url",
    "requested_url",
    "title",
    "author",
    "published",
    "status",
    "text",
    "text_format",
    "text_path",
    "metadata_path",
    "text_hash",
    "text_characters",
    "coverage",
    "capture_coverage",
    "captured_at",
    "more_below",
    "error",
    "notice",
)
CANDIDATE_FIELDS = (
    "id",
    "rank",
    "url",
    "title",
    "status",
    "selected",
    "text_path",
    "metadata_path",
    "text_characters",
    "error",
    "notice",
)
RESULT_FIELDS = (
    "status",
    "query",
    "search_receipt",
    "requested_candidates",
    "candidate_count",
    "capture_complete",
    "discovery_complete",
    "discovery_limitation",
    "elapsed_seconds",
    "read_instructions",
    "error",
    "review",
)


def page_document(page):
    """New capture metadata is not automatically prompt data."""
    view = {key: page[key] for key in PAGE_FIELDS if key in page}
    if "text_format" not in view:
        view["text_format"] = "legacy_plain_text"
        view["representation_note"] = (
            "Original text is unchanged. Legacy link targets and table-span "
            "metadata remain available through metadata_path; no DOM inventory "
            "is appended to this document."
        )
    return view


def result_document(raw):
    if not isinstance(raw, dict) or "search_receipt" not in raw:
        return raw
    view = {key: raw[key] for key in RESULT_FIELDS if key in raw}
    if isinstance(raw.get("page"), dict):
        view["page"] = page_document(raw["page"])
    if "selected" in raw:
        view["selected"] = [page_document(page) for page in raw["selected"]]
    if "candidates" in raw:
        # Decisions already occur once in review.sources.
        view["candidates"] = [
            {key: page[key] for key in CANDIDATE_FIELDS if key in page}
            for page in raw["candidates"]
        ]
    return view
