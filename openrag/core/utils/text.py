"""Text sanitization utilities for cleaning extracted text.

Pure functions — no infrastructure imports. Used by chunking, indexing,
and document processing pipelines.

Moved from: components/indexer/utils/text_sanitizer.py
"""

import re
import unicodedata

DEFAULT_FALLBACK_ENCODING = "utf-8"


def decode_bytes(raw: bytes, encoding: str | None = None) -> str:
    """Decode ``raw`` to ``str`` with a UTF-8-first detection strategy.

    chardet alone misclassifies short ASCII-heavy UTF-8 as Latin-1, which
    produces mojibake on common short inputs. Trying strict UTF-8 first
    catches the common case; chardet handles genuinely non-UTF-8 inputs.
    Falls back to UTF-8 with ``errors="replace"`` so this never raises.
    """
    if encoding:
        return raw.decode(encoding, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import chardet
    except ImportError:
        return raw.decode(DEFAULT_FALLBACK_ENCODING, errors="replace")
    guess = chardet.detect(raw)
    detected = guess.get("encoding") or DEFAULT_FALLBACK_ENCODING
    return raw.decode(detected, errors="replace")


def sanitize_text(
    text: str,
    normalize_whitespace: bool = True,
    remove_control_chars: bool = True,
    remove_zero_width_chars: bool = True,
    max_consecutive_newlines: int = 2,
    normalize_unicode: bool = True,
) -> str:
    """Sanitize text by removing useless characters and normalizing whitespace.

    Performs comprehensive text cleaning including:
    - Removing or normalizing control characters
    - Removing zero-width spaces and invisible characters
    - Normalizing excessive whitespace (spaces, tabs)
    - Limiting consecutive newlines
    - Unicode normalization

    Args:
        text: The input text to sanitize
        normalize_whitespace: If True, normalize spaces and tabs to single spaces
        remove_control_chars: If True, remove control characters (except \\n, \\r, \\t)
        remove_zero_width_chars: If True, remove zero-width spaces and similar chars
        max_consecutive_newlines: Maximum number of consecutive newlines to keep (0 = unlimited)
        normalize_unicode: If True, normalize unicode to NFC form

    Returns:
        Sanitized text string
    """
    if not text:
        return text

    if normalize_unicode:
        text = unicodedata.normalize("NFC", text)

    if remove_zero_width_chars:
        text = re.sub(r"[\u200B-\u200D\u2060\uFEFF]", "", text)

    if remove_control_chars:
        text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]", "", text)

    if normalize_whitespace:
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\t+", " ", text)
        text = re.sub(r"(?m)^ +", "", text)
        text = re.sub(r"(?m) +$", "", text)

    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\r", "\n", text)

    if max_consecutive_newlines > 0:
        pattern = r"\n{" + str(max_consecutive_newlines + 1) + r",}"
        replacement = "\n" * max_consecutive_newlines
        text = re.sub(pattern, replacement, text)

    text = text.strip()
    return text


def clean_markdown_table_spacing(markdown_table: str) -> str:
    """Normalize spacing inside a markdown table.

    Trims each cell while keeping table shape intact.
    """
    cleaned_lines = []

    for line in markdown_table.strip().split("\n"):
        if "|" not in line:
            cleaned_lines.append(line.strip())
            continue

        parts = line.split("|")
        cleaned_cells = [cell.strip() for cell in parts]
        new_line = "| " + " | ".join(cleaned_cells[1:-1]) + " |"
        cleaned_lines.append(new_line)

    return "\n".join(cleaned_lines)


def sanitize_extracted_text(text: str) -> str:
    """Convenience function for sanitizing text extracted from documents.

    Applies default sanitization settings suitable for text extraction
    endpoints and general document processing.
    """
    return sanitize_text(text)
