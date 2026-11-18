"""The ``web_search`` step: every query searched on the web, the results deduplicated."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.utils.web_url import normalize_web_url

if TYPE_CHECKING:
    from core.websearch import WebResult
    from services.websearch import WebSearchService


class WebSearch:
    """Searches several queries at once over *service* (``WebSearchService``).

    The service never raises: a failed or unconfigured search returns no results.
    """

    def __init__(self, service: WebSearchService) -> None:
        self._service = service

    @property
    def max_tokens(self) -> int:
        """The most tokens the web results may take in a prompt."""
        return self._service.max_tokens

    async def search(self, queries: Sequence[str]) -> list[WebResult]:
        """The results of all *queries*, in parallel, first seen first, one per normalized URL."""
        return dedupe_web(await asyncio.gather(*[self._service.search(query) for query in queries]))


def dedupe_web(web_lists: list[list]) -> list:
    """The results of *web_lists* in order, one per URL, invalid URLs dropped.

    Each kept result's URL is replaced with its normalized form, before the
    results are numbered as sources.
    """
    seen: set[str] = set()
    out: list = []
    for r in (r for lst in web_lists for r in lst):
        url = normalize_web_url(r.url)
        if url is not None and url not in seen:
            r.url = url
            seen.add(url)
            out.append(r)
    return out
