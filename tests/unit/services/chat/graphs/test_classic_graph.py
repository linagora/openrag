"""Unit tests for the ``classic`` graph's wiring (``services.chat.graphs.classic``).

The graph runs with the real branch decisions of ``ClassicSteps`` and node
bodies that only record that they ran, so each test reads as the path a kind
of request takes. What each step does is tested through ``ChatService`` in
``tests/unit/services/orchestrators/test_chat_service.py`` and the chat
characterization snapshots.
"""

from __future__ import annotations

import pytest
from core.chat.routing import Route
from core.chat.scope import Scope
from core.chat.smalltalk import CasualMessagePolicy
from core.models.chat import ChatOptions
from core.models.query import Query, SearchQueries
from core.utils.exceptions import ContextWindowExceededError
from services.chat.classic import ChatContext, ClassicSteps
from services.chat.graphs.classic import build_classic_graph

_QUERIES = SearchQueries(query_list=[Query(query="q")])
_HELLO = CasualMessagePolicy("greeting", "en")


class RecordingSteps(ClassicSteps):
    """Node bodies that record their order and return the updates a test scripts."""

    def __init__(self, **updates: dict) -> None:
        self.ran: list[str] = []
        self._updates = updates

    async def _record(self, name: str) -> dict:
        self.ran.append(name)
        update = self._updates.get(name, {})
        if isinstance(update, Exception):
            raise update
        return update

    async def route(self, state, ctx):
        return await self._record("route")

    async def contextualize(self, state, ctx):
        return await self._record("contextualize")

    async def trace_config(self, state, ctx):
        return await self._record("trace_config")

    async def small_talk(self, state, ctx):
        return await self._record("small_talk")

    async def retrieve(self, state, ctx):
        return await self._record("retrieve")

    async def budget(self, state, ctx):
        return await self._record("budget")

    async def summarize(self, state, ctx):
        return await self._record("summarize")

    async def prompt(self, state, ctx):
        return await self._record("prompt")


def _context(*, route: Route = Route.DOCUMENTS, **metadata) -> ChatContext:
    return ChatContext(
        options=ChatOptions.from_metadata(metadata),
        route=route,
        scope=Scope(partitions=None if route is Route.WEB else ["p"]),
        llm=object(),
        payload={"messages": []},
    )


async def _run(steps: RecordingSteps, ctx: ChatContext) -> list[str]:
    await build_classic_graph(steps).ainvoke(
        {"messages": [{"role": "user", "content": "q"}], "custom_prompt": None, "last_user_message": "q"},
        context=ctx,
    )
    return steps.ran


_NOT_CASUAL = {"casual": None, "acknowledgement_candidate": False, "retrieval_forced": False}
_FOUND = {"queries": _QUERIES, "chunks": ["chunk"], "web_results": []}


@pytest.mark.asyncio
async def test_a_question_is_contextualized_retrieved_and_fitted():
    steps = RecordingSteps(route=_NOT_CASUAL, contextualize={"queries": _QUERIES}, retrieve=_FOUND)

    assert await _run(steps, _context()) == ["route", "contextualize", "trace_config", "retrieve", "budget", "prompt"]


@pytest.mark.asyncio
async def test_small_talk_skips_the_contextualizer_and_the_search():
    steps = RecordingSteps(route={"casual": _HELLO, "acknowledgement_candidate": False, "retrieval_forced": False})

    assert await _run(steps, _context()) == ["route", "trace_config", "small_talk"]


@pytest.mark.asyncio
async def test_an_acknowledgement_the_contextualizer_confirms_is_answered_as_small_talk():
    steps = RecordingSteps(
        route={"casual": None, "acknowledgement_candidate": True, "retrieval_forced": False},
        contextualize={"queries": SearchQueries(query_list=[]), "casual": CasualMessagePolicy("acknowledgement", "en")},
    )

    assert await _run(steps, _context()) == ["route", "contextualize", "trace_config", "small_talk"]


@pytest.mark.asyncio
async def test_forced_retrieval_searches_even_for_small_talk():
    steps = RecordingSteps(
        route={"casual": _HELLO, "acknowledgement_candidate": False, "retrieval_forced": True},
        contextualize={"queries": _QUERIES},
        retrieve=_FOUND,
    )

    ran = await _run(steps, _context(require_retrieval=True))

    assert ran == ["route", "contextualize", "trace_config", "retrieve", "budget", "prompt"]


@pytest.mark.asyncio
async def test_bypass_reaches_the_contextualize_step_even_for_small_talk():
    # The step records the bypass on the trace; the request is still small talk.
    steps = RecordingSteps(route={"casual": _HELLO, "acknowledgement_candidate": False, "retrieval_forced": False})

    ran = await _run(steps, _context(bypass_query_contextualization=True))

    assert ran == ["route", "contextualize", "trace_config", "small_talk"]


@pytest.mark.asyncio
async def test_a_web_only_search_that_finds_nothing_ends_without_a_prompt():
    steps = RecordingSteps(
        route=_NOT_CASUAL,
        contextualize={"queries": _QUERIES},
        retrieve={"queries": _QUERIES, "chunks": [], "web_results": []},
    )

    assert await _run(steps, _context(route=Route.WEB)) == ["route", "contextualize", "trace_config", "retrieve"]


@pytest.mark.asyncio
async def test_documents_found_nothing_still_build_the_prompt():
    # The model is told no document was retrieved rather than answering blind.
    steps = RecordingSteps(
        route=_NOT_CASUAL,
        contextualize={"queries": _QUERIES},
        retrieve={"queries": _QUERIES, "chunks": [], "web_results": []},
    )

    ran = await _run(steps, _context())

    assert ran == ["route", "contextualize", "trace_config", "retrieve", "budget", "prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("docs", "summarized"), [(["doc"], True), ([], False)])
async def test_map_reduce_runs_only_when_asked_and_there_is_something_to_summarize(docs, summarized):
    steps = RecordingSteps(
        route={**_NOT_CASUAL, "retrieval_forced": True},
        contextualize={"queries": _QUERIES},
        retrieve=_FOUND,
        budget={"docs": docs},
    )

    ran = await _run(steps, _context(use_map_reduce=True))

    assert ("summarize" in ran) is summarized
    assert ran[-1] == "prompt"


@pytest.mark.asyncio
async def test_a_step_error_reaches_the_caller_unchanged():
    # The router maps our own exceptions to status codes (413 here).
    steps = RecordingSteps(
        route=_NOT_CASUAL,
        contextualize={"queries": _QUERIES},
        retrieve=_FOUND,
        budget=ContextWindowExceededError(5000, 4000),
    )

    with pytest.raises(ContextWindowExceededError):
        await _run(steps, _context())
    assert "prompt" not in steps.ran
