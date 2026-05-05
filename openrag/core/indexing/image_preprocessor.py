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
import logging
import re
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Markdown image-reference patterns (compile once; shared regex objects)
# ---------------------------------------------------------------------------

HTTP_IMAGE_PATTERN = re.compile(r"!\[(.*?)\]\((https?://[^)]+)\)")
DATA_URI_IMAGE_PATTERN = re.compile(r"!\[(.*?)\]\((data:image/[^;]+;base64,[^)]+)\)")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Qwen2.5-VL ``min_pixels`` threshold; images below this break the model.
MIN_IMAGE_PIXELS = 784


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
        return base64.b64decode(b64)
    except Exception as exc:
        logger.warning("Failed to decode data URI: %s", exc)
        return None


def mime_from_data_uri(data_uri: str) -> str:
    """Pull the mime type out of a data URI; fall back to ``image/png``.

    Example: ``data:image/jpeg;base64,xxx`` → ``image/jpeg``.
    """
    try:
        return data_uri.split(",", 1)[0].split(":", 1)[1].split(";", 1)[0]
    except Exception:
        return "image/png"


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
    from ..models.document import ImageBlock

    blocks: list[Any] = []
    for alt, data_uri in DATA_URI_IMAGE_PATTERN.findall(text):
        payload = decode_data_uri(data_uri)
        if payload is None:
            continue
        blocks.append(
            ImageBlock(
                image_bytes=payload,
                page_number=page_number,
                mime_type=mime_from_data_uri(data_uri),
                metadata={"markdown_ref": f"![{alt}]({data_uri})", "alt": alt},
            )
        )
    return blocks
