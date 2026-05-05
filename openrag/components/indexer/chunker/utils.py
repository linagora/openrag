"""Backward-compatibility shim — re-exports from `openrag.core.chunking.markdown_utils`.

The implementation moved to `openrag/core/chunking/markdown_utils.py` in
Phase 5B. New code should import from there directly. This file is kept
so existing legacy imports keep working until the consumers migrate;
scheduled for removal in Phase 12.
"""

from openrag.core.chunking.markdown_utils import (
    IMAGE_RE,
    PAGE_RE,
    TABLE_RE,
    MDElement,
    chunk_table,
    get_chunk_page_number,
    get_page_number,
    parse_markdown_table,
    span_inside,
    split_md_elements,
)

__all__ = [
    "IMAGE_RE",
    "MDElement",
    "PAGE_RE",
    "TABLE_RE",
    "chunk_table",
    "get_chunk_page_number",
    "get_page_number",
    "parse_markdown_table",
    "span_inside",
    "split_md_elements",
]
