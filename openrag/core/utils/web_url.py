"""Shared validation and normalization for displayable web URLs."""

from urllib.parse import urlparse

import httpx
from core.utils.text import sanitize_text


def normalize_web_url(value: object) -> str | None:
    """Return a renderable HTTP(S) URL, or None when the value is invalid."""
    if not isinstance(value, str):
        return None
    url = sanitize_text(value)
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return str(httpx.URL(url))
    except (ValueError, httpx.InvalidURL):
        return None


__all__ = ["normalize_web_url"]
