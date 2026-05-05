"""Text preprocessing utilities for the indexing pipeline.

Re-exports the canonical implementations from `core.utils.text`. Kept as a
named entry point under `core.indexing` so callers can import preprocessing
helpers alongside parsers, validators, and contextualization without
reaching into the generic utils package.
"""

from ..utils.text import clean_markdown_table_spacing, decode_bytes, sanitize_extracted_text, sanitize_text

__all__ = [
    "clean_markdown_table_spacing",
    "sanitize_extracted_text",
    "sanitize_text",
    "decode_bytes",
]
