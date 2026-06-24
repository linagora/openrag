"""Text sanitization utilities for cleaning extracted text.

Pure functions — no infrastructure imports. Used by chunking, indexing,
and document processing pipelines.

Moved from: components/indexer/utils/text_sanitizer.py
"""

import re
import unicodedata

from core.config import load_config
from core.utils.logging import get_logger

DEFAULT_FALLBACK_ENCODING = "utf-8"

logger = get_logger()


_cached_length_function = None


def get_num_tokens():
    """Return the configured token counter, with a local tiktoken fallback."""
    global _cached_length_function
    if _cached_length_function is None:
        try:
            from langchain_openai import ChatOpenAI

            config = load_config()
            llm = ChatOpenAI(**config.llm.model_dump())
            _cached_length_function = llm.get_num_tokens
        except Exception as exc:
            import tiktoken

            logger.warning(
                "ChatOpenAI unavailable for token counting, falling back to tiktoken cl100k_base",
                error=str(exc),
            )
            encoding = tiktoken.get_encoding("cl100k_base")
            _cached_length_function = lambda text: len(encoding.encode(text))  # noqa: E731
    return _cached_length_function


def decode_bytes(raw: bytes, encoding: str | None = None) -> str:
    """Decode ``raw`` to ``str`` with a UTF-8-first detection strategy.

    chardet alone misclassifies short ASCII-heavy UTF-8 as Latin-1, which
    produces mojibake on common short inputs. Trying strict UTF-8 first
    catches the common case; chardet handles genuinely non-UTF-8 inputs.
    Falls back to UTF-8 with ``errors="replace"`` so this never raises.
    """
    if encoding:
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            # Invalid codec name — fall through to detection.
            pass
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
    try:
        return raw.decode(detected, errors="replace")
    except LookupError:
        return raw.decode(DEFAULT_FALLBACK_ENCODING, errors="replace")


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


# Our RAG context uses a few control tokens that an attacker could embed in a
# document to forge citations or fake source boundaries:
#   - "[Source N]"        : the per-chunk block marker prepended in format_context
#   - "[Sources: 1, 3]"   : the answer tag the LLM appends and we parse back
#                           (the parser also accepts the unbracketed form
#                            "Sources: 1, 3" at end-of-line)
#   - "----------"        : the inter-source separator (SOURCE_SEPARATOR)
# Neutralize them inside untrusted chunk/web text so they can only originate
# from our own formatter, never from document content.
# The block regex captures an optional trailing colon: the answer parser's
# bracket is optional (\[?), so neutralizing only "[" would leave
# "(Sources: 1, 2]" — still a parser match. Dropping the colon defangs the
# keyword the parser keys on.
_INJECT_SOURCE_BLOCK_RE = re.compile(r"\[\s*(sources?)\b(\s*:)?", re.IGNORECASE)
_INJECT_SOURCES_TAG_RE = re.compile(r"(?im)^([ \t]*)(sources?)(\s*:\s*)(\[?[\d,\s]+\]?)[ \t]*$")
_INJECT_SEPARATOR_RE = re.compile(r"-{4,}")


def neutralize_prompt_control_tokens(text: str) -> str:
    """Defang RAG control tokens that appear inside untrusted text.

    Keeps the text human-readable while ensuring an embedded ``[Source 5]``,
    ``[Sources: 1, 2]`` / ``Sources: 1, 2`` or ``----------`` separator can no
    longer be mistaken for a marker our pipeline produced.
    """
    if not text:
        return text
    # "[Source...]" / "[Sources...]" -> open paren so it can't start a marker,
    # and drop any "[Sources:" colon so the answer-tag parser can't match the
    # remainder.
    text = _INJECT_SOURCE_BLOCK_RE.sub(lambda m: "(" + m.group(1) + (" " if m.group(2) else ""), text)
    # Break the unbracketed line-terminal "Sources: 1, 2" form the answer parser
    # also matches, by replacing the colon.
    text = _INJECT_SOURCES_TAG_RE.sub(r"\1\2 \4", text)
    # Cap long hyphen runs so a chunk can't reproduce the source separator.
    text = _INJECT_SEPARATOR_RE.sub("---", text)
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
