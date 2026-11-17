"""The agent's tools over the documents: search them all, search one file, read around a passage, summarize.

Searching *locates*: it returns passages, numbered ``[Source N]``, best match
first, the best-ranked in full (``ToolContext.full_passages``, the agent
profile's) and the others as excerpts, so a model that answers without reading
still answers from the best passages in full. Reading proves: ``read_file``
returns a passage in full with the ones around it in the file. Both go through chat retrieval, so the partition's
retrieval preset (reranker, ``top_n``) applies as in ``classic``, and both stay
within the request's scope (``ToolContext.scope``), whatever the model asks for.
``summarize`` condenses passages for a question with ``classic``'s map-reduce.
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
from core.tools.passages import excerpt, passage, passages
from core.utils.exceptions import PartitionNotFoundError
from pydantic import Field

if TYPE_CHECKING:
    from core.retrieval.trace import RetrievalTraceBuilder
    from services.capabilities.retrieve import SourceRetriever
    from services.capabilities.summarize import Summarizer
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.retrieval_service import RetrievalService

#: Characters of a chunk a search shows: enough to judge it, short enough to search widely.
DEFAULT_EXCERPT_CHARS = 400

#: Passages ``read_file`` may add on each side of the one asked for.
MAX_NEIGHBOURS = 3

# What the model is shown of each tool, sent with every reply it makes: what the
# tool does and what only it needs. How the tools work together is the prompt's.
_SEARCH_KNOWLEDGE = (
    "Search the documents. Returns passages numbered [Source N], best match first; its first line says which "
    "are in full and which are excerpts. The queries of a call share its dates: parts of the question with "
    "different periods take separate calls."
)
_SEARCH_FILE = "Search inside one file, named by the partition and file id shown with a source."
_READ_FILE = "Read a [Source N] in full, with the passages around it in its file, in reading order."
_SUMMARIZE = (
    'Only for a question about a file as a whole, e.g. "what is this report about?": summarizes the file '
    "(partition and file id), or the [Source N] of its passages. For any other question, read the passages "
    "instead. Returns the relevant ones as short summaries, each under its [Source N]."
)

#: Sources ``summarize`` takes in one call.
MAX_SUMMARIZED_SOURCES = 20

Queries = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=500)]],
    Field(
        min_length=1,
        max_length=3,
        description=(
            "1 to 3 queries, one per distinct part of the question, not rephrasings of one. Each is a full "
            "question or sentence in the documents' language, not keywords: the search ranks passages by meaning. "
            "It doesn't see the conversation: keep the user's wording, pronouns replaced by what they name. "
            "Write every abbreviation or acronym with its full form in parentheses after it: the search misses "
            "documents that only use the other form. "
            'E.g. "How much is invested in AI (artificial intelligence)?", not "AI investment".'
        ),
    ),
]
CreatedAfter = Annotated[
    date | None, Field(description="Only documents created on or after this date (YYYY-MM-DD, UTC).")
]
CreatedBefore = Annotated[
    date | None, Field(description="Only documents created before this date, which is excluded (YYYY-MM-DD, UTC).")
]
Order = Annotated[
    DateOrder | None,
    Field(
        description='"newest" for "the last X", "oldest" for "the first X"; the query then names the kind '
        'of document, e.g. "meeting notes".'
    ),
]
Neighbours = Annotated[int, Field(ge=0, le=MAX_NEIGHBOURS)]


def created_between(after: date | None, before: date | None) -> list[TemporalPredicate]:
    """The search's filter on the documents' own date: on or after *after*, before *before*."""
    return [
        TemporalPredicate(operator=operator, value=day.isoformat())
        for operator, day in ((">=", after), ("<", before))
        if day is not None
    ]


class DocumentTools:
    """``search_knowledge``, ``search_file``, ``read_file`` and ``summarize``.

    *summarizer* is ``classic``'s map-reduce, which runs on the static LLM.
    """

    def __init__(
        self,
        *,
        retriever: SourceRetriever,
        retrieval_service: RetrievalService,
        prompt_settings: PartitionPromptSettings,
        summarizer: Summarizer,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> None:
        self._retriever = retriever
        self._retrieval = retrieval_service
        self._prompt_settings = prompt_settings
        self._summarizer = summarizer
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
        dates = created_between(created_after, created_before)
        search = SearchQueries(
            query_list=[Query(query=text, temporal_filters=dates or None, date_order=order) for text in queries]
        )
        chunks = await self._search(search, ctx.scope, trace=ctx.trace)
        if not chunks:
            advice = "the question reworded, with the terms the documents would use and abbreviations spelled out"
            return f"No passage matched. Try {advice}{', or wider dates' if dates else ''}."
        # The best-ranked are shown in full whatever order they are shown in.
        return self._located(ctx, order_chunks_by_date(chunks, order), ranked=chunks, order=order)

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
        chunks = await self._search(search, Scope(partitions=[partition], file_ids=[file_id]), trace=ctx.trace)
        if not chunks:
            return f"No passage of file '{file_id}' matched. Try other words."
        return self._located(ctx, chunks, ranked=chunks, of=f" of file '{file_id}'")

    @tool("read_file", _READ_FILE)
    async def read_file(
        self,
        ctx: ToolContext,
        source: Annotated[int, Field(ge=1, description="The N of a [Source N] a search returned.")],
        before: Annotated[Neighbours, Field(description="Passages to add before it.")] = 1,
        after: Annotated[Neighbours, Field(description="Passages to add after it.")] = 1,
    ) -> str:
        chunk = self._document_source(ctx, source)
        earlier, later = await self._neighbours(chunk, before=before, after=after)
        blocks = [passage(ctx.sources.number(c, key=c.id), c) for c in [*earlier, chunk, *later]]
        return (
            f"[Source {source}] in full, with {len(earlier)} passage(s) before and {len(later)} after:\n\n"
            + passages(blocks)
        )

    @tool("summarize", _SUMMARIZE, timeout=120)
    async def summarize(
        self,
        ctx: ToolContext,
        question: Annotated[str, Field(min_length=1, max_length=500, description="What the summaries are for.")],
        sources: Annotated[
            list[Annotated[int, Field(ge=1)]] | None,
            Field(min_length=1, max_length=MAX_SUMMARIZED_SOURCES, description="The N of each [Source N] to condense."),
        ] = None,
        partition: Annotated[str | None, Field(description="With file_id, the file to condense.")] = None,
        file_id: Annotated[str | None, Field(description="With partition, the file to condense.")] = None,
    ) -> str:
        if (sources is None) == (partition is None and file_id is None):
            raise ToolError("Give either sources or a file (partition and file_id), not both.")
        if sources is not None:
            chunks = [self._document_source(ctx, number) for number in dict.fromkeys(sources)]
        else:
            if partition is None or file_id is None:
                raise ToolError("A file is named by both its partition and its file_id.")
            if not ctx.allows_file(partition, file_id):
                raise ToolError(
                    f"File '{file_id}' of partition '{partition}' is not among this conversation's documents."
                )
            search = SearchQueries(query_list=[Query(query=question)])
            chunks = await self._search(search, Scope(partitions=[partition], file_ids=[file_id]), trace=ctx.trace)
            if not chunks:
                return f"No passage of file '{file_id}' matches the question."

        documents = [chunk.to_langchain() for chunk in chunks]
        kept = await self._summarizer.summarize(question, documents)
        # The summaries keep their chunk's metadata, its _id among them; a chunk
        # the map-reduce could not judge comes back as it was (fail-open).
        by_document = {id(document): chunk for document, chunk in zip(documents, chunks, strict=True)}
        by_id = {chunk.id: chunk for chunk in chunks}
        blocks = []
        for document in kept:
            if (chunk := by_document.get(id(document))) is not None:
                text = f"(Not summarized.) {excerpt(chunk.text, self._excerpt_chars)}"
            else:
                chunk = by_id[str(document.metadata["_id"])]
                text = document.page_content
            number = ctx.sources.number(chunk, key=chunk.id)
            blocks.append(passage(number, chunk.model_copy(update={"text": text})))
        left_out = len(chunks) - len(blocks)
        if not blocks:
            return f"None of the {len(chunks)} passages is relevant to the question."
        heading = f"{len(blocks)} of {len(chunks)} passages are relevant to the question, each summarized"
        heading += f"; {left_out} not relevant." if left_out else "."
        return f"{heading}\n\n{passages(blocks)}"

    def _document_source(self, ctx: ToolContext, number: int) -> Chunk:
        """The document chunk shown as ``[Source number]``, refused when it is missing, a web page or out of scope."""
        chunk = ctx.sources.get(number)
        if chunk is None:
            raise ToolError(f"There is no [Source {number}] yet: use the numbers a search returned.")
        if not isinstance(chunk, Chunk):
            raise ToolError(f"[Source {number}] is a web page, not a document.")
        if not ctx.allows_file(chunk.partition, chunk.document_id):
            raise ToolError(f"[Source {number}] is not among this conversation's documents.")
        return chunk

    async def _search(
        self, search: SearchQueries, scope: Scope, *, trace: RetrievalTraceBuilder | None = None
    ) -> list[Chunk]:
        """The chunks for *search* in *scope*, cut to the ``top_n`` its partitions' presets set.

        With *trace* (the call's, when the request is traced), the search records its stages into it.
        """
        try:
            chunks, _web = await self._retriever.retrieve(
                search,
                route=Route.DOCUMENTS,
                scope=scope,
                top_k=None,
                retrieval_top_k=None,
                overrides={},
                trace=trace,
            )
        except PartitionNotFoundError as e:
            raise ToolError(e.message) from e
        return chunks[: self._prompt_settings.top_n(scope.partitions)]

    def _located(
        self, ctx: ToolContext, chunks: list[Chunk], *, ranked: list[Chunk], order: str | None = None, of: str = ""
    ) -> str:
        """*chunks* as a search shows them: the ``ctx.full_passages`` best of *ranked* in full, the others as excerpts.

        The first line says which are which: after an *order* by date the ones
        in full aren't the first shown.
        """
        # TODO: pick the passages shown in full by their scores instead of a fixed
        # count: those the reranker (or, without one, the retrieval) scored close
        # to the best hit, so a clear winner comes alone and a close field comes in
        # full. Relative to the best, since the scores' scale depends on the
        # provider; the retrieval's own score still needs keeping (`_dict_to_chunk`
        # drops it).
        full = {chunk.id for chunk in ranked[: ctx.full_passages]}
        blocks = [
            passage(ctx.sources.number(c, key=c.id), c, excerpt_chars=None if c.id in full else self._excerpt_chars)
            for c in chunks
        ]
        shown = f"{order} document first" if order else "best match first"
        if not full:
            heading = f"{len(chunks)} excerpts{of}, {shown}."
        elif len(full) == len(chunks):
            heading = f"{len(chunks)} passages{of} in full, {shown}."
        else:
            if order:
                which = "the best match" if len(full) == 1 else f"the {len(full)} best matches"
            else:
                which = "the first" if len(full) == 1 else f"the first {len(full)}"
            rest = "the other as an excerpt" if len(chunks) - len(full) == 1 else "the others as excerpts"
            heading = f"{len(chunks)} passages{of}, {shown}: {which} in full, {rest}."
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
