"""Web search over several queries: in parallel, one result per URL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.websearch import WebResult
from services.capabilities.web_search import WebSearch, dedupe_web


def test_dedupe_web_preserves_first_seen():
    a = SimpleNamespace(url="https://example.test/one")
    b = SimpleNamespace(url="https://example.test/one")
    c = SimpleNamespace(url="https://example.test/two")
    assert dedupe_web([[a, b], [c]]) == [a, c]


def test_dedupe_web_drops_invalid_urls_before_source_numbering():
    invalid = SimpleNamespace(url="javascript:alert(1)")
    valid = SimpleNamespace(url="https://example.test/evidence")

    assert dedupe_web([[invalid, valid]]) == [valid]


class FakeService:
    max_tokens = 1234

    def __init__(self, results: dict[str, list[WebResult]]):
        self._results = results
        self.queries: list[str] = []

    async def search(self, query):
        self.queries.append(query)
        return list(self._results.get(query, []))


@pytest.mark.asyncio
async def test_every_query_is_searched_and_the_results_follow_the_query_order():
    shared = WebResult(title="Shared", url="https://example.test/shared", snippet="")
    service = FakeService(
        {
            "first": [WebResult(title="A", url="https://example.test/a", snippet=""), shared],
            "second": [WebResult(title="Shared again", url="https://example.test/shared", snippet="")],
        }
    )
    web = WebSearch(service)

    results = await web.search(["first", "second"])

    assert service.queries == ["first", "second"]
    assert [r.title for r in results] == ["A", "Shared"]
    assert web.max_tokens == 1234
