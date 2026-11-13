"""The agent's tools over the documents: search them all, search one file, read around a passage.

Searching *locates*: it returns excerpts, numbered ``[Source N]``, best match
first. Reading proves: ``read_file`` returns a passage in full with the ones
around it in the file. Both go through chat retrieval, so the partition's
retrieval preset (reranker, ``top_n``) applies as in ``classic``, and both stay
within the request's scope (``ToolContext.scope``), whatever the model asks for.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated

from core.chat.routing import Route
from core.chat.scope import Scope
from core.models.chunk import Chunk
from core.models.query import DateOrder, Query, SearchQueries, TemporalPredicate
from core.retrieval.date_order import order_chunks_by_date
from core.tools import ToolContext, ToolError, tool
from core.tools.passages import passage, passages
from core.utils.exceptions import PartitionNotFoundError
from pydantic import Field

if TYPE_CHECKING:
    from services.capabilities.retrieve import SourceRetriever
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.retrieval_service import RetrievalService

#: Characters of a chunk a search shows: enough to judge it, short enough to search widely.
DEFAULT_EXCERPT_CHARS = 400

#: Passages ``read_file`` may add on each side of the one asked for.
MAX_NEIGHBOURS = 3

_SEARCH_KNOWLEDGE = (
    "Search the documents this conversation can see. Send 1 to 3 queries in one call when the question "
    "has distinct parts; they are searched together. Keep the distinctive words, in the documents' language. "
    "Put dates in created_after / created_before, not in the queries. Returns excerpts numbered [Source N], "
    "best match first: they locate information; read_file reads one in full before you rely on it."
)
_SEARCH_FILE = (
    "Search inside one file, named by the partition and file id shown with each source. "
    "Returns excerpts numbered [Source N], best match first."
)
_READ_FILE = (
    "Read in full a passage a search returned, [Source N], with the passages just before and after it "
    "in its file, in reading order. Read a passage before relying on its excerpt."
)

Queries = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=500)]],
    Field(min_length=1, max_length=3, description="1 to 3 search queries, one per distinct need."),
]
CreatedAfter = Annotated[
    date | None, Field(description="Only documents created on or after this date (YYYY-MM-DD, UTC).")
]
CreatedBefore = Annotated[
    date | None, Field(description="Only documents created before this date, which is excluded (YYYY-MM-DD, UTC).")
]
Order = Annotated[
    DateOrder | None,
    Field(description='Show the newest or oldest documents first, e.g. "newest" for "the last meeting".'),
]
Neighbours = Annotated[int, Field(ge=0, le=MAX_NEIGHBOURS)]


class DocumentTools:
    """``search_knowledge``, ``search_file`` and ``read_file``."""

    def __init__(
        self,
        *,
        retriever: SourceRetriever,
        retrieval_service: RetrievalService,
        prompt_settings: PartitionPromptSettings,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> None:
        self._retriever = retriever
        self._retrieval = retrieval_service
        self._prompt_settings = prompt_settings
        self._excerpt_chars = excerpt_chars

    @tool("search_knowledge", _SEARCH_KNOWLEDGE)
    async def search_knowledge(
        self,
        ctx: ToolContext,
        queries: Queries,
        created_after: CreatedAfter = None,
        created_before: CreatedBefore = None,
        order: Order = None,
    ) -> str:
        if not ctx.scope.partitions:
            raise ToolError("This conversation has no documents to search.")
        if created_after and created_before and created_after >= created_before:
            raise ToolError("created_after must be earlier than created_before.")
        dates = [
            TemporalPredicate(operator=operator, value=day.isoformat())
            for operator, day in ((">=", created_after), ("<", created_before))
            if day is not None
        ]
        search = SearchQueries(
            query_list=[Query(query=text, temporal_filters=dates or None, date_order=order) for text in queries]
        )
        chunks = await self._search(search, ctx.scope)
        if not chunks:
            advice = "fewer, more distinctive words, in the documents' language"
            return f"No passage matched. Try other queries: {advice}{', or wider dates' if dates else ''}."
        shown = f"{order} document first" if order else "best match first"
        return self._located(ctx, order_chunks_by_date(chunks, order), f"{len(chunks)} excerpts, {shown}.")

    @tool("search_file", _SEARCH_FILE)
    async def search_file(
        self,
        ctx: ToolContext,
        partition: Annotated[str, Field(description="The partition shown with a source.")],
        file_id: Annotated[str, Field(description="The file id shown with a source.")],
        queries: Queries,
    ) -> str:
        if not ctx.allows_file(partition, file_id):
            raise ToolError(f"File '{file_id}' of partition '{partition}' is not among this conversation's documents.")
        search = SearchQueries(query_list=[Query(query=text) for text in queries])
        chunks = await self._search(search, Scope(partitions=[partition], file_ids=[file_id]))
        if not chunks:
            return f"No passage of file '{file_id}' matched. Try other words."
        return self._located(ctx, chunks, f"{len(chunks)} excerpts of file '{file_id}', best match first.")

    @tool("read_file", _READ_FILE)
    async def read_file(
        self,
        ctx: ToolContext,
        source: Annotated[int, Field(ge=1, description="The N of a [Source N] a search returned.")],
        before: Annotated[Neighbours, Field(description="Passages to add before it.")] = 1,
        after: Annotated[Neighbours, Field(description="Passages to add after it.")] = 1,
    ) -> str:
        chunk = ctx.sources.get(source)
        if chunk is None:
            raise ToolError(f"There is no [Source {source}] yet: read a source a search returned.")
        if not isinstance(chunk, Chunk):
            raise ToolError(f"[Source {source}] is a web page, not a document.")
        if not ctx.allows_file(chunk.partition, chunk.document_id):
            raise ToolError(f"[Source {source}] is not among this conversation's documents.")
        earlier, later = await self._neighbours(chunk, before=before, after=after)
        blocks = [passage(ctx.sources.number(c, key=c.id), c) for c in [*earlier, chunk, *later]]
        return (
            f"[Source {source}] in full, with {len(earlier)} passage(s) before and {len(later)} after:\n\n"
            + passages(blocks)
        )

    async def _search(self, search: SearchQueries, scope: Scope) -> list[Chunk]:
        """The chunks for *search* in *scope*, cut to the ``top_n`` its partitions' presets set."""
        try:
            chunks, _web = await self._retriever.retrieve(
                search, route=Route.DOCUMENTS, scope=scope, top_k=None, retrieval_top_k=None, overrides={}
            )
        except PartitionNotFoundError as e:
            raise ToolError(e.message) from e
        return chunks[: self._prompt_settings.top_n(scope.partitions)]

    def _located(self, ctx: ToolContext, chunks: list[Chunk], heading: str) -> str:
        blocks = [passage(ctx.sources.number(c, key=c.id), c, excerpt_chars=self._excerpt_chars) for c in chunks]
        return f"{heading}\n\n{passages(blocks)}"

    async def _neighbours(self, chunk: Chunk, *, before: int, after: int) -> tuple[list[Chunk], list[Chunk]]:
        """Up to *before* passages preceding *chunk* in its file and *after* following it, in reading order.

        Each passage links to the next by its section ids, so the file is
        walked one step at a time on each side; a side ends at the file's edge
        or at a link that leads nowhere (a damaged or partly deleted file).
        """
        earlier: list[Chunk] = []
        later: list[Chunk] = []
        left: Chunk | None = chunk
        right: Chunk | None = chunk
        for step in range(max(before, after)):
            left = left if step < before and left is not None and _link(left, "prev") is not None else None
            right = right if step < after and right is not None and _link(right, "next") is not None else None
            if left is None and right is None:
                break
            edges = list({c.id: c for c in (left, right) if c is not None}.values())
            found = {
                c.metadata.get("section_id"): c
                for c in await self._retrieval.surrounding_chunks(edges, partition=chunk.partition)
                if c.document_id == chunk.document_id
            }
            if left is not None:
                left = found.get(_link(left, "prev"))
                if left is not None:
                    earlier.insert(0, left)
            if right is not None:
                right = found.get(_link(right, "next"))
                if right is not None:
                    later.append(right)
        return earlier, later


def _link(chunk: Chunk, side: str) -> object:
    return chunk.metadata.get(f"{side}_section_id")


__all__ = ["DocumentTools"]
