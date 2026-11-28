"""Text sanitization utilities for cleaning extracted text.

Pure functions — no infrastructure imports. Used by chunking, indexing,
and document processing pipelines.

Moved from: components/indexer/utils/text_sanitizer.py
"""

import re
import unicodedata

from core.config import load_config
from core.utils.logging import get_logger
from core.utils.source_filtering import neutralize_sources_tags

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
            llm_kwargs = config.llm.model_dump()
            llm_kwargs.pop("enable_thinking", None)
            llm = ChatOpenAI(**llm_kwargs)
            if llm.custom_get_token_ids is None:
                # Keep LangChain's model/encoding selection, but count literal
                # control-token spellings as ordinary user text.
                _, encoding = llm._get_encoding_model()
                llm.custom_get_token_ids = encoding.encode_ordinary
            _cached_length_function = llm.get_num_tokens
        except Exception as exc:
            import tiktoken

            logger.warning(
                "ChatOpenAI unavailable for token counting, falling back to tiktoken cl100k_base",
                error_type=type(exc).__name__,
            )
            encoding = tiktoken.get_encoding("cl100k_base")
            _cached_length_function = lambda text: len(encoding.encode_ordinary(text))  # noqa: E731
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


# A line that may open or close a fenced code block: its run of ` or ~, then the rest.
_CODE_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


class CodeFenceTracker:
    """Tells, line by line, what is inside a fenced code block.

    A fence closes as CommonMark closes it: on a line of the character that
    opened it, at least as long as the opening run, with nothing after it. A
    shorter run, or the other character, is a line of the listing, so a
    four-backtick block that shows a three-backtick block stays one block. A
    fence left open runs to the end of the text.
    """

    def __init__(self) -> None:
        self._opening: str | None = None

    @property
    def in_fence(self) -> bool:
        return self._opening is not None

    def feed(self, line: str) -> str:
        """``"fence"`` for a line that opens or closes a block, ``"code"`` for
        a line inside one, ``"text"`` for any other."""
        marker = _CODE_FENCE_LINE_RE.match(line)
        if self._opening is None:
            # The info string of a backtick fence holds no backtick: ```x``` is inline code.
            if marker is None or (marker[1][0] == "`" and "`" in marker[2]):
                return "text"
            self._opening = marker[1]
            return "fence"
        closes = (
            marker is not None
            and marker[1][0] == self._opening[0]
            and len(marker[1]) >= len(self._opening)
            and not marker[2].strip()
        )
        if not closes:
            return "code"
        self._opening = None
        return "fence"


def strip_keeping_first_indent(text: str) -> str:
    """``str.strip`` that leaves the indentation of the first line in place.

    A chunk that starts in the middle of a code listing opens on an indented
    line. Stripped like prose, that line moves to column zero and, in YAML or
    Python, to another level than the lines under it. Blank lines before the
    first line and everything after the last one are still removed. For a text
    with no leading spaces it is ``str.strip``.
    """
    return re.sub(r"\A\s*\n", "", text).rstrip()


def _normalize_whitespace(text: str, keep_code_indentation: bool) -> str:
    """Collapse runs of spaces and tabs, and trim each line.

    With *keep_code_indentation*, the lines of a fenced code block only lose
    their trailing spaces: their indentation is what the code means (a YAML
    mapping's nesting, a Python block), and their inner spacing is part of it.
    """
    if not keep_code_indentation:
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\t+", " ", text)
        text = re.sub(r"(?m)^ +", "", text)
        return re.sub(r"(?m) +$", "", text)
    lines: list[str] = []
    fences = CodeFenceTracker()
    for line in text.split("\n"):
        if fences.feed(line) == "code":
            lines.append(line.rstrip(" "))
        else:
            lines.append(re.sub(r"\t+", " ", re.sub(r" {2,}", " ", line)).strip(" "))
    return "\n".join(lines)


def sanitize_text(
    text: str,
    normalize_whitespace: bool = True,
    remove_control_chars: bool = True,
    remove_zero_width_chars: bool = True,
    max_consecutive_newlines: int = 2,
    normalize_unicode: bool = True,
    keep_code_indentation: bool = False,
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
        keep_code_indentation: If True, whitespace normalization leaves the
            indentation and inner spacing of fenced code blocks untouched

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
        text = _normalize_whitespace(text, keep_code_indentation)

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
_INJECT_SEPARATOR_RE = re.compile(r"-{4,}")


def neutralize_prompt_control_tokens(text: str) -> str:
    """Defang RAG control tokens that appear inside untrusted text.

    Keeps the text human-readable while ensuring an embedded ``[Source 5]``,
    ``[Sources: 1, 2]`` / ``Sources: 1, 2`` or ``----------`` separator can no
    longer be mistaken for a marker our pipeline produced.
    """
    if not text:
        return text
    # Keep source-marker and citation-label neutralization beside the parser so
    # its accepted forms cannot drift from this boundary.
    text = neutralize_sources_tags(text)
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
