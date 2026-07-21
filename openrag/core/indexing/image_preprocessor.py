"""Image preprocessing helpers for the indexing pipeline.

Pure helpers — no VLM, no langchain, no infrastructure imports. Used by
parsers (core) and Ray-pool adapters (services) that need to:

- normalize PIL Image modes for PNG encoding
- encode PIL Images as PNG bytes or base64 data URIs
- detect / decode markdown image references (HTTP / data URI) in extracted text

Extracted from the legacy ``components/indexer/loaders/base.py``; the
legacy module is kept as a back-compat shim until existing imports are
migrated.
"""

from __future__ import annotations

import base64
import hashlib
import re
from io import BytesIO
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Markdown image-reference patterns (compile once; shared regex objects)
# ---------------------------------------------------------------------------

HTTP_IMAGE_PATTERN = re.compile(r"!\[(.*?)\]\((https?://[^)]+)\)")
DATA_URI_IMAGE_PATTERN = re.compile(
    r"!\[([^]]*)\]\((<?data:image/(?:[^)'\"]|'[^']*'|\"[^\"]*\")*)\)",
    re.IGNORECASE,
)
DATA_URI_LINK_PATTERN = re.compile(r"(?<!!)\[([^]]*)\]\(<?data:image/[^)]*\)", re.IGNORECASE)
DATA_URI_REFERENCE_IMAGE_PATTERN = re.compile(r"!\[([^]]*)\](?:\[([^]]*)\]|(?!\())")
DATA_URI_REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^[ \t]{0,3}\[([^]\r\n]+)\]:[ \t]*"
    r"(<?data:image/[^\r\n]*(?:(?<!=)\r?\n[ \t]*[A-Za-z0-9+/]+={0,2})*)$",
    re.IGNORECASE | re.MULTILINE,
)
_RESIDUAL_DATA_URI_PREFIX_PATTERN = re.compile(r"data:image/", re.IGNORECASE)
_VALID_DATA_URI_TARGET_PATTERN = re.compile(
    r"^(data:image/[^;,\s)]+(?:;[^;,=\s)]+=[^;,=\s)]*)*;base64,"
    r"[A-Za-z0-9+/=]+(?:[ \t\r\n\f\v]+[A-Za-z0-9+/=]+)*)"
    r"(?:[ \t\r\n\f\v]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\)\r\n]*\)))?"
    r"[ \t\r\n\f\v]*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Qwen2.5-VL ``min_pixels`` threshold; images below this break the model.
MIN_IMAGE_PIXELS = 784
MAX_EMBEDDED_IMAGES = 50
MAX_EMBEDDED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EMBEDDED_TOTAL_BYTES = 100 * 1024 * 1024
_URI_DELIMITERS = frozenset("\"'<>()[]{}")


# ---------------------------------------------------------------------------
# PIL mode normalization & encoding
# ---------------------------------------------------------------------------


def ensure_png_compatible_mode(image: Any) -> Any:
    """Convert PIL image modes that PNG can't encode directly.

    CMYK/YCbCr/LAB → RGB; P/LA/PA → RGBA. Others returned unchanged.
    """
    if image.mode in ("CMYK", "YCbCr", "LAB"):
        return image.convert("RGB")
    if image.mode in ("P", "LA", "PA"):
        return image.convert("RGBA")
    return image


def pil_to_png_bytes(image: Any) -> bytes:
    """Encode a PIL Image as PNG bytes. ``bytes`` input is passed through."""
    if isinstance(image, bytes):
        return image
    image = ensure_png_compatible_mode(image)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# URL / data URI detection
# ---------------------------------------------------------------------------


def decode_data_uri(data_uri: str) -> bytes | None:
    """Decode a ``data:image/...;base64,...`` URI into raw bytes. ``None`` on failure."""
    try:
        _, b64 = data_uri.split(",", 1)
        return base64.b64decode("".join(b64.split()), validate=True)
    except Exception as exc:
        logger.bind(error=str(exc)).warning("Failed to decode data URI")
        return None


def mime_from_data_uri(data_uri: str) -> str:
    """Pull the mime type out of a data URI; fall back to ``image/png``.

    Example: ``data:image/jpeg;base64,xxx`` → ``image/jpeg``.
    """
    try:
        return data_uri.split(",", 1)[0].split(":", 1)[1].split(";", 1)[0]
    except Exception:
        return "image/png"


def _data_uri_from_markdown_target(target: str) -> str | None:
    """Return a validated data URI without an optional Markdown title."""
    if target.startswith("<"):
        closing_bracket = target.find(">")
        if closing_bracket == -1:
            return None
        target = target[1:closing_bracket] + target[closing_bracket + 1 :]
    match = _VALID_DATA_URI_TARGET_PATTERN.fullmatch(target)
    return match.group(1) if match else None


def _estimated_base64_size(encoded: str) -> int:
    """Estimate decoded bytes exactly for valid padded Base64."""
    padding = min(len(encoded) - len(encoded.rstrip("=")), 2)
    return max(0, (len(encoded) * 3) // 4 - padding)


def _normalize_reference_label(label: str) -> str:
    """Normalize a Markdown reference label for case-insensitive matching."""
    return " ".join(label.split()).casefold()


def _contextual_data_uri_end(text: str, start: int, closing_delimiter: str) -> int:
    """Scan to a closing delimiter or blank-line paragraph boundary."""
    cursor = start
    while cursor < len(text):
        if text[cursor] == closing_delimiter:
            return cursor
        if text[cursor] in "\r\n":
            next_line = cursor + 1
            if text[cursor] == "\r" and next_line < len(text) and text[next_line] == "\n":
                next_line += 1
            while next_line < len(text) and text[next_line] in " \t":
                next_line += 1
            if next_line < len(text) and text[next_line] in "\r\n":
                return cursor
        cursor += 1
    return len(text)


def _residual_data_uri_end(text: str, start: int, prefix_end: int) -> int:
    """Find the end of an unhandled data URI without backtracking."""
    if start > 0 and text[start - 1] in {'"', "'", "<", "("}:
        closing_delimiter = {'"': '"', "'": "'", "<": ">", "(": ")"}[text[start - 1]]
        return _contextual_data_uri_end(text, prefix_end, closing_delimiter)

    end = prefix_end
    while end < len(text) and not text[end].isspace() and text[end] not in _URI_DELIMITERS:
        end += 1
    return end


def _scrub_unterminated_parenthesized_data_uris(text: str) -> tuple[str, int]:
    """Remove unterminated parenthesized data URIs before Markdown regexes run."""
    parts: list[str] = []
    cursor = 0
    search_from = 0
    scrubbed = 0

    while match := _RESIDUAL_DATA_URI_PREFIX_PATTERN.search(text, search_from):
        opening_delimiter = match.start() - 1
        if opening_delimiter >= 0 and text[opening_delimiter] == "<":
            opening_delimiter -= 1
        if opening_delimiter < 0 or text[opening_delimiter] != "(":
            search_from = match.end()
            continue

        end = _contextual_data_uri_end(text, match.end(), ")")
        if end < len(text) and text[end] == ")":
            search_from = end + 1
            continue

        parts.append(text[cursor : match.start()])
        parts.append("[Image]")
        cursor = end
        search_from = end
        scrubbed += 1

    if not scrubbed:
        return text, 0
    parts.append(text[cursor:])
    return "".join(parts), scrubbed


def _scrub_residual_data_uris(text: str) -> tuple[str, int]:
    """Replace data URIs left by syntax-aware extraction using a linear scan."""
    parts: list[str] = []
    cursor = 0
    scrubbed = 0

    while match := _RESIDUAL_DATA_URI_PREFIX_PATTERN.search(text, cursor):
        parts.append(text[cursor : match.start()])
        parts.append("[Image]")
        cursor = _residual_data_uri_end(text, match.start(), match.end())
        scrubbed += 1

    if not scrubbed:
        return text, 0
    parts.append(text[cursor:])
    return "".join(parts), scrubbed


def extract_data_uri_image_blocks(text: str, *, page_number: int = 1) -> list[Any]:
    """Build ``ImageBlock``s for every ``![alt](data:image/...;base64,...)`` ref.

    The original markdown ref is preserved in ``metadata['markdown_ref']``
    so a downstream caption stage can substitute the wrapped caption back
    into the corresponding ``TextBlock`` via ``str.replace``.

    Returns ``list[ImageBlock]`` (declared as ``list[Any]`` only because
    importing the model would create a cycle in some build orderings —
    the caller side is type-correct).
    """
    if not text:
        return []
    # Local import to avoid a top-level cycle with ``core.models``.
    from core.models.document import ImageBlock

    blocks: list[Any] = []
    for match in DATA_URI_IMAGE_PATTERN.finditer(text):
        alt, target = match.groups()
        data_uri = _data_uri_from_markdown_target(target)
        if data_uri is None:
            continue
        payload = decode_data_uri(data_uri)
        if payload is None:
            continue
        blocks.append(
            ImageBlock(
                image_bytes=payload,
                page_number=page_number,
                mime_type=mime_from_data_uri(data_uri),
                metadata={"markdown_ref": match.group(0), "alt": alt},
            )
        )
    return blocks


def normalize_data_uri_images(
    text: str,
    *,
    page_number: int = 1,
    max_images: int = MAX_EMBEDDED_IMAGES,
    max_image_bytes: int = MAX_EMBEDDED_IMAGE_BYTES,
    max_total_bytes: int = MAX_EMBEDDED_TOTAL_BYTES,
    reference_scope: str | None = None,
) -> tuple[str, list[Any]]:
    """Extract embedded images and remove image data URIs from Markdown.

    Accepted images receive a compact deterministic placeholder that remains
    compatible with the parser-to-caption replacement contract. Rejected or
    malformed images and data-URI links are reduced to their display text.
    Inline and reference-style images share the same validation and limits, so
    raw payloads never continue into chunking. ``reference_scope`` keeps
    placeholders unique when independently parsed documents are combined.
    """
    if not text:
        return text, []

    from core.models.document import ImageBlock

    text, unterminated_scrubbed = _scrub_unterminated_parenthesized_data_uris(text)
    blocks: list[Any] = []
    matched = 0
    skipped = 0
    total_bytes = 0
    existing_targets = set(re.findall(r"\(openrag-embedded-image-[^)]+\)", text))
    generated_targets: set[str] = set()

    def replace_target(alt: str, target: str) -> str:
        nonlocal matched, skipped, total_bytes
        matched += 1
        fallback = alt.strip() or "[Image]"

        if matched > max_images:
            skipped += 1
            return fallback

        data_uri = _data_uri_from_markdown_target(target)
        if data_uri is None:
            skipped += 1
            return fallback

        encoded = "".join(data_uri.split(",", 1)[1].split()) if "," in data_uri else ""
        estimated_bytes = _estimated_base64_size(encoded)
        if estimated_bytes > max_image_bytes or total_bytes + estimated_bytes > max_total_bytes:
            skipped += 1
            return fallback

        payload = decode_data_uri(data_uri)
        if payload is None or len(payload) > max_image_bytes or total_bytes + len(payload) > max_total_bytes:
            skipped += 1
            return fallback

        digest_seed = f"{matched}:{alt}" if reference_scope is None else f"{reference_scope}:{matched}:{alt}"
        digest_builder = hashlib.sha256(digest_seed.encode())
        digest_builder.update(payload)
        digest = digest_builder.hexdigest()[:16]
        suffix = 0
        while True:
            disambiguator = f"-{suffix}" if suffix else ""
            target = f"(openrag-embedded-image-{digest}{disambiguator})"
            if target not in existing_targets and target not in generated_targets:
                break
            suffix += 1
        generated_targets.add(target)
        placeholder = f"![{alt}]{target}"
        blocks.append(
            ImageBlock(
                image_bytes=payload,
                page_number=page_number,
                mime_type=mime_from_data_uri(data_uri),
                metadata={"markdown_ref": placeholder, "alt": alt},
            )
        )
        total_bytes += len(payload)
        return placeholder

    reference_targets: dict[str, str] = {}
    for match in DATA_URI_REFERENCE_DEFINITION_PATTERN.finditer(text):
        label, target = match.groups()
        reference_targets.setdefault(_normalize_reference_label(label), target.strip())

    def replace_reference(match: re.Match[str]) -> str:
        alt, label = match.groups()
        target = reference_targets.get(_normalize_reference_label(label or alt))
        return match.group(0) if target is None else replace_target(alt, target)

    sanitized = DATA_URI_IMAGE_PATTERN.sub(lambda match: replace_target(*match.groups()), text)
    sanitized = DATA_URI_REFERENCE_IMAGE_PATTERN.sub(replace_reference, sanitized)
    sanitized = DATA_URI_REFERENCE_DEFINITION_PATTERN.sub("", sanitized)
    sanitized = DATA_URI_LINK_PATTERN.sub(lambda match: match.group(1).strip() or "[Link]", sanitized)
    sanitized, residual_scrubbed = _scrub_residual_data_uris(sanitized)
    if skipped:
        logger.bind(skipped=skipped, matched=matched).warning(
            "Skipped embedded image(s) while sanitizing document text"
        )
    if scrubbed := unterminated_scrubbed + residual_scrubbed:
        logger.bind(scrubbed=scrubbed).warning("Scrubbed residual embedded image data URI(s)")
    return sanitized, blocks
