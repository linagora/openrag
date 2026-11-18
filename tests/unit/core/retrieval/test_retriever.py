"""Retriever tests with a fake searcher.

These exercise the retriever without Ray, OpenAI, or LangChain — proving the
core/ retriever has clean dependencies.
"""

from __future__ import annotations

import asyncio

import pytest
from core.models.chunk import Chunk
from core.retrieval.retriever import Retriever
from core.retrieval.searcher import RetrievalSearcher


class FakeSearcher(RetrievalSearcher):
    """Records calls; returns canned chunks."""

    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.related_calls: list[dict] = []
        self.ancestor_calls: list[dict] = []
        self.csv_row_calls: list[dict] = []
        self.search_result: list[Chunk] = []
        self.related_result: list[Chunk] = []
        self.ancestor_result: list[Chunk] = []
        self.csv_row_result: list[Chunk] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.search_result)

    async def get_surrounding_chunks(self, **kwargs):
        return []

    async def get_csv_row_chunks(self, **kwargs):
        self.csv_row_calls.append(kwargs)
        return list(self.csv_row_result)

    async def get_related_chunks(self, **kwargs):
        self.related_calls.append(kwargs)
        return list(self.related_result)

    async def get_ancestor_chunks(self, **kwargs):
        self.ancestor_calls.append(kwargs)
        return list(self.ancestor_result)


def _chunk(idv: str, text: str = "x", document_id: str = "", partition: str = "p1") -> Chunk:
    return Chunk(id=idv, text=text, document_id=document_id, partition=partition)


@pytest.mark.asyncio
async def test_single_retriever_passes_through_to_searcher():
    s = FakeSearcher()
    s.search_result = [_chunk("1"), _chunk("2")]
    r = Retriever(searcher=s, top_k=4, similarity_threshold=0.3, with_surrounding_chunks=False)
    out = await r.retrieve(partition=["p1"], query="hello", filter="x>0", filter_params={"a": 1})
    assert [c.id for c in out] == ["1", "2"]
    assert s.search_calls == [
        {
            "query": "hello",
            "partition": ["p1"],
            "top_k": 4,
            "filter": "x>0",
            "filter_params": {"a": 1},
            "similarity_threshold": 0.3,
            "with_surrounding_chunks": False,
        }
    ]


@pytest.mark.asyncio
async def test_expansion_disabled_returns_unchanged():
    s = FakeSearcher()
    r = Retriever(searcher=s)
    initial = [_chunk("1")]
    out = await r.expand_search_results(initial)
    assert out is initial
    assert not s.related_calls
    assert not s.ancestor_calls


@pytest.mark.asyncio
async def test_expansion_with_related_dedupes_by_id():
    s = FakeSearcher()
    s.related_result = [_chunk("1"), _chunk("3")]  # "1" already in results
    r = Retriever(searcher=s, include_related=True)
    initial = [
        Chunk(id="1", text="x", partition="p1", metadata={"relationship_id": "r1"}),
    ]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1", "3"]


@pytest.mark.asyncio
async def test_expansion_passes_the_search_filter():
    s = FakeSearcher()
    r = Retriever(searcher=s, include_related=True, include_ancestors=True)
    initial = [Chunk(id="1", text="x", partition="p1", document_id="f1", metadata={"relationship_id": "r1"})]
    expr = 'created_at >= ISO "2026-01-01T00:00:00+00:00"'
    await r.expand_search_results(initial, filter=expr)
    assert s.related_calls[0]["filter"] == expr
    assert s.ancestor_calls[0]["filter"] == expr


@pytest.mark.asyncio
async def test_expansion_with_ancestors_calls_searcher():
    s = FakeSearcher()
    s.ancestor_result = [_chunk("99", document_id="f1")]
    r = Retriever(searcher=s, include_ancestors=True, related_limit=20, max_ancestor_depth=2)
    initial = [Chunk(id="1", text="x", partition="p1", document_id="f1")]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1", "99"]
    assert s.ancestor_calls[0]["partition"] == "p1"
    assert s.ancestor_calls[0]["file_id"] == "f1"
    assert s.ancestor_calls[0]["limit"] == 20
    assert s.ancestor_calls[0]["max_ancestor_depth"] == 2


@pytest.mark.asyncio
async def test_expansion_preserves_unlimited_ancestor_depth():
    searcher = FakeSearcher()
    retriever = Retriever(searcher=searcher, include_ancestors=True, max_ancestor_depth=None)

    await retriever.expand_search_results([Chunk(id="1", text="x", partition="p1", document_id="f1")])

    assert searcher.ancestor_calls[0]["max_ancestor_depth"] is None


@pytest.mark.asyncio
async def test_expansion_fetches_and_orders_csv_continuations():
    s = FakeSearcher()
    s.csv_row_result = [
        Chunk(
            id="part-3",
            text="three",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 30, "csv_part": 3, "csv_parts_total": 3},
        ),
        Chunk(
            id="part-1",
            text="one",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 30, "csv_part": 1, "csv_parts_total": 3},
        ),
        Chunk(
            id="part-2",
            text="two",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 30, "csv_part": 2, "csv_parts_total": 3},
        ),
    ]
    initial = [s.csv_row_result[-1]]

    out = await Retriever(searcher=s).expand_search_results(initial)

    assert [chunk.id for chunk in out] == ["part-1", "part-2", "part-3"]
    assert s.csv_row_calls == [
        {"partition": "p1", "file_id": "file-1", "row_number": 30, "limit": 10, "allowed_file_ids": None}
    ]


@pytest.mark.asyncio
async def test_csv_sibling_expansion_respects_related_limit():
    s = FakeSearcher()
    initial = Chunk(
        id="part-1",
        text="one",
        document_id="file-1",
        partition="p1",
        metadata={
            "csv_row_number": 30,
            "csv_part": 1,
            "csv_parts_total": 14,
            "csv_row_chunks_total": 14,
        },
    )
    s.csv_row_result = [initial]

    await Retriever(searcher=s, related_limit=10).expand_search_results([initial])

    assert s.csv_row_calls[0]["limit"] == 10


@pytest.mark.asyncio
async def test_csv_sibling_expansion_passes_search_filter():
    s = FakeSearcher()
    initial = Chunk(
        id="part-1",
        text="one",
        document_id="file-1",
        partition="p1",
        metadata={"csv_row_number": 30, "csv_part": 1, "csv_parts_total": 2},
    )

    await Retriever(searcher=s).expand_search_results([initial], filter='category == "public"')

    assert s.csv_row_calls[0]["filter"] == 'category == "public"'


@pytest.mark.asyncio
async def test_csv_sibling_expansion_respects_concurrency_limit():
    import asyncio

    from core.retrieval.retriever import MAX_EXPANSION_CONCURRENCY

    searcher = FakeSearcher()
    active = 0
    maximum = 0

    async def lookup(**kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    searcher.get_csv_row_chunks = lookup
    chunks = [
        Chunk(
            id=f"row-{row}",
            text="value",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": row, "csv_part": 1, "csv_parts_total": 2},
        )
        for row in range(MAX_EXPANSION_CONCURRENCY + 5)
    ]
    await Retriever(searcher=searcher).expand_search_results(chunks)
    assert maximum == MAX_EXPANSION_CONCURRENCY


@pytest.mark.asyncio
async def test_csv_sibling_expansion_is_disabled_when_related_limit_is_zero():
    s = FakeSearcher()
    initial = Chunk(
        id="part-1",
        text="one",
        document_id="file-1",
        partition="p1",
        metadata={
            "csv_row_number": 30,
            "csv_part": 1,
            "csv_parts_total": 2,
            "csv_row_chunks_total": 2,
        },
    )
    s.csv_row_result = [initial]

    await Retriever(searcher=s, related_limit=0).expand_search_results([initial])

    assert not s.csv_row_calls


@pytest.mark.asyncio
async def test_expansion_fetches_and_orders_csv_column_groups():
    s = FakeSearcher()
    s.csv_row_result = [
        Chunk(
            id="group-3",
            text="third group",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 40, "csv_column_group": 3, "csv_column_groups_total": 3},
        ),
        Chunk(
            id="group-1",
            text="first group",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 40, "csv_column_group": 1, "csv_column_groups_total": 3},
        ),
        Chunk(
            id="group-2",
            text="second group",
            document_id="file-1",
            partition="p1",
            metadata={"csv_row_number": 40, "csv_column_group": 2, "csv_column_groups_total": 3},
        ),
    ]

    out = await Retriever(searcher=s).expand_search_results([s.csv_row_result[0]])

    assert [chunk.id for chunk in out] == ["group-1", "group-2", "group-3"]
    assert s.csv_row_calls == [
        {"partition": "p1", "file_id": "file-1", "row_number": 40, "limit": 10, "allowed_file_ids": None}
    ]


@pytest.mark.asyncio
async def test_spreadsheet_sibling_expansion_is_scoped_to_its_sheet():
    s = FakeSearcher()
    initial = Chunk(
        id="sheet-1-part-1",
        text="first part",
        document_id="workbook-1",
        partition="p1",
        metadata={
            "table_id": "sheet:1",
            "csv_row_number": 3,
            "csv_part": 1,
            "csv_parts_total": 2,
        },
    )
    s.csv_row_result = [initial]

    await Retriever(searcher=s).expand_search_results([initial])

    assert s.csv_row_calls == [
        {
            "partition": "p1",
            "file_id": "workbook-1",
            "row_number": 3,
            "limit": 10,
            "allowed_file_ids": None,
            "table_id": "sheet:1",
        }
    ]


@pytest.mark.asyncio
async def test_csv_sibling_expansion_skips_chunks_without_document_id():
    s = FakeSearcher()
    initial = [
        Chunk(
            id="missing-file",
            text="continuation",
            partition="p1",
            metadata={"csv_row_number": 50, "csv_part": 1, "csv_parts_total": 2},
        )
    ]

    out = await Retriever(searcher=s).expand_search_results(initial)

    assert out == initial
    assert s.csv_row_calls == []


@pytest.mark.asyncio
async def test_expansion_swallows_per_call_errors():
    class BoomSearcher(FakeSearcher):
        async def get_related_chunks(self, **kwargs):
            raise RuntimeError("kaboom")

    s = BoomSearcher()
    r = Retriever(searcher=s, include_related=True)
    initial = [Chunk(id="1", text="x", partition="p1", metadata={"relationship_id": "r1"})]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1"]


@pytest.mark.asyncio
async def test_expansion_swallows_ancestor_errors():
    class BoomSearcher(FakeSearcher):
        async def get_ancestor_chunks(self, **kwargs):
            raise RuntimeError("ancestor exploded")

    s = BoomSearcher()
    r = Retriever(searcher=s, include_ancestors=True)
    initial = [Chunk(id="1", text="x", partition="p1", document_id="f1")]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1"]


@pytest.mark.asyncio
async def test_expansion_preserves_all_unique_results():
    searcher = FakeSearcher()
    searcher.related_result = [_chunk(f"related-{index}") for index in range(1_001)]
    retriever = Retriever(searcher=searcher, include_related=True, related_limit=100)
    initial = [Chunk(id="initial", text="x", partition="p1", metadata={"relationship_id": "r1"})]

    expanded = await retriever.expand_search_results(initial)

    assert len(expanded) == 1_002


@pytest.mark.asyncio
async def test_expansion_bounds_lookup_concurrency():
    class ConcurrencySearcher(FakeSearcher):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def get_related_chunks(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return []

    searcher = ConcurrencySearcher()
    retriever = Retriever(searcher=searcher, include_related=True)
    initial = [
        Chunk(
            id=f"chunk-{index}",
            text="x",
            partition="p1",
            metadata={"relationship_id": f"relationship-{index}"},
        )
        for index in range(20)
    ]

    await retriever.expand_search_results(initial)

    assert searcher.max_active == 16


@pytest.mark.asyncio
async def test_expansion_dedupes_ancestor_fetches_per_file():
    """Two chunks from the same (partition, document_id) must enqueue a
    single ancestor fetch, not two."""

    class CountingSearcher(FakeSearcher):
        def __init__(self) -> None:
            super().__init__()
            self.ancestor_call_count = 0

        async def get_ancestor_chunks(self, **kwargs):
            self.ancestor_call_count += 1
            return list(self.ancestor_result)

    s = CountingSearcher()
    s.ancestor_result = [_chunk("99", document_id="f1")]
    r = Retriever(searcher=s, include_ancestors=True)
    initial = [
        Chunk(id="1", text="x", partition="p1", document_id="f1"),
        Chunk(id="2", text="y", partition="p1", document_id="f1"),
        Chunk(id="3", text="z", partition="p1", document_id="f1"),
    ]
    await r.expand_search_results(initial)
    assert s.ancestor_call_count == 1
