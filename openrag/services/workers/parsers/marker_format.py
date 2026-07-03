"""Ray-free Marker output formatting — the single source of truth shared by the
Ray ``MarkerLoader`` and the off-Ray ``MarkerServeClient``, so both produce an
identical ``ProcessedDocument`` shape (one TextBlock per page + ImageBlocks).
"""

from __future__ import annotations

import base64
import re

from core.indexing.image_preprocessor import pil_to_png_bytes
from core.models.document import ImageBlock
from core.utils.logging import get_logger

logger = get_logger()

PAGE_SEP = "[PAGE_SEP]"
_PAGE_MARKER_RE = re.compile(r"\{(\d+)\}" + re.escape(PAGE_SEP))
_MARKER_KEY_PAGE_RE = re.compile(r"_page_(\d+)_")


def marker_key_to_page(key: str) -> int | None:
    """1-indexed page from a Marker image key (``_page_0_Picture_1.jpeg`` -> 1)."""
    match = _MARKER_KEY_PAGE_RE.search(key)
    if match is None:
        return None
    try:
        return int(match.group(1)) + 1
    except (TypeError, ValueError):
        return None


def split_pages(markdown: str) -> list[tuple[int, str]]:
    """Clean Marker output and split into ``[(page_number, text), …]``.

    Marker emits ``…{1}[PAGE_SEP]…{2}[PAGE_SEP]…``; we drop the leading segment,
    strip ``<br>``, and split on each ``{N}[PAGE_SEP]`` marker. Blank pages are
    preserved so page numbering reflects the source. Markdown with no markers
    collapses to a single page-1 entry.
    """
    if markdown is None:
        return []
    if PAGE_SEP in markdown:
        markdown = markdown.split(PAGE_SEP, 1)[1]
    markdown = markdown.replace("<br>", "")

    pairs: list[tuple[int, str]] = []
    cursor = 0
    last_page = 0
    for match in _PAGE_MARKER_RE.finditer(markdown):
        page = int(match.group(1))
        text = markdown[cursor : match.start()].strip()
        pairs.append((page, text))
        cursor = match.end()
        last_page = page
    tail = markdown[cursor:].strip()
    if tail:
        pairs.append((last_page + 1, tail))
    elif not pairs and markdown.strip():
        pairs.append((1, markdown.strip()))
    return pairs


def _image_block(png_bytes: bytes, key: str) -> ImageBlock:
    return ImageBlock(
        image_bytes=png_bytes,
        page_number=marker_key_to_page(str(key)),
        mime_type="image/png",
        metadata={"markdown_ref": f"![]({key})", "marker_key": str(key)},
    )


def build_image_blocks(images: dict) -> list[ImageBlock]:
    """From Marker's in-process ``{key: PIL_image}`` (Ray path)."""
    blocks: list[ImageBlock] = []
    for key, pil_image in images.items():
        try:
            blocks.append(_image_block(pil_to_png_bytes(pil_image), key))
        except Exception as exc:  # noqa: BLE001 — skip one bad image, keep the rest
            logger.warning(f"Failed to encode Marker image {key}: {exc}")
    return blocks


def build_image_blocks_from_encoded(images: dict[str, str]) -> list[ImageBlock]:
    """From marker-serve's ``{key: base64-png}`` result (off-Ray path)."""
    blocks: list[ImageBlock] = []
    for key, b64 in images.items():
        try:
            blocks.append(_image_block(base64.b64decode(b64), key))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to decode Marker image {key}: {exc}")
    return blocks


__all__ = [
    "PAGE_SEP",
    "marker_key_to_page",
    "split_pages",
    "build_image_blocks",
    "build_image_blocks_from_encoded",
]
