from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from core.utils.text import sanitize_text


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    content: str | None = None


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


class BaseWebSearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[WebResult]:
        """Return web results for the query. May raise on failure."""
        ...
