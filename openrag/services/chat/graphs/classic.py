"""The ``classic`` graph: which step runs after which.

The only code that sees LangGraph. Each node unpacks the request's context and
calls its step in ``services.chat.classic``; the edges decide which step comes
next. Every path ends with the answer's prompt ready (or, for a request with
nothing to search, or a web-only one whose search found nothing, the request
left as it was), and ``ChatService`` then asks ``AnswerService`` for the
answer, outside the graph.

    START ─┬─ no partition, no web search → direct → END
           └─ scope → route ─┬─ small talk ───────────────┐
                             └─ contextualize ────────────┤
                                                          ▼
                                                    trace_config
                                              ┌───────────┴──────────┐
                                         small_talk → END         retrieve
                                                           web only, nothing found → END
                                                                     ▼
                                                                  budget ─ map-reduce ─→ summarize
                                                                     ▼                      ▼
                                                                  prompt  ◀─────────────────┘
                                                                     ▼
                                                                    END
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from core.chat.state import ClassicState
from services.chat.classic import ChatContext, ClassicSteps
from services.chat.graphs.step_log import log_step

# Importing langgraph warns (pending deprecation) that its checkpoint
# serializer's defaults will change. No graph uses a checkpointer, and with
# LOG_FORMAT=json the warning would be a stray non-JSON line on stderr at
# startup. The import's warnings are recorded rather than filtered, because the
# LangChain packages it pulls in register filters that show their own
# warnings; any other warning is raised again as it was.
with warnings.catch_warnings(record=True) as _import_warnings:
    from langgraph.graph import END, START, StateGraph
    from langgraph.runtime import Runtime
for _warning in _import_warnings:
    if not (issubclass(_warning.category, PendingDeprecationWarning) and "allowed_objects" in str(_warning.message)):
        warnings.warn_explicit(_warning.message, _warning.category, _warning.filename, _warning.lineno)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def build_classic_graph(steps: ClassicSteps) -> CompiledStateGraph:
    """Compile the ``classic`` graph over *steps*; compiled once, run for every chat request."""

    async def direct(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "direct")
        return await steps.direct(state, runtime.context)

    async def scope(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "scope")
        return await steps.scope(state, runtime.context)

    async def route(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "route")
        return await steps.route(state, runtime.context)

    async def contextualize(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "contextualize")
        return await steps.contextualize(state, runtime.context)

    async def trace_config(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "trace_config")
        return await steps.trace_config(state, runtime.context)

    async def small_talk(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "small_talk")
        return await steps.small_talk(state, runtime.context)

    async def retrieve(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        queries = state.get("queries")
        log_step("classic", "retrieve", queries=len(queries.query_list) if queries else 0)
        return await steps.retrieve(state, runtime.context)

    async def budget(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "budget")
        return await steps.budget(state, runtime.context)

    async def summarize(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "summarize")
        return await steps.summarize(state, runtime.context)

    async def prompt(state: ClassicState, runtime: Runtime[ChatContext]) -> dict:
        log_step("classic", "prompt")
        return await steps.prompt(state, runtime.context)

    def after_start(state: ClassicState, runtime: Runtime[ChatContext]) -> str:
        return "direct" if steps.is_direct(runtime.context) else "scope"

    def after_route(state: ClassicState, runtime: Runtime[ChatContext]) -> str:
        return "contextualize" if steps.needs_queries(state, runtime.context) else "trace_config"

    def after_trace_config(state: ClassicState) -> str:
        return "small_talk" if steps.is_small_talk(state) else "retrieve"

    def after_retrieve(state: ClassicState, runtime: Runtime[ChatContext]) -> str:
        return END if steps.found_nothing_on_the_web(state, runtime.context) else "budget"

    def after_budget(state: ClassicState, runtime: Runtime[ChatContext]) -> str:
        return "summarize" if steps.map_reduce_requested(state, runtime.context) else "prompt"

    graph = StateGraph(ClassicState, context_schema=ChatContext)
    for node in (direct, scope, route, contextualize, trace_config, small_talk, retrieve, budget, summarize, prompt):
        graph.add_node(node.__name__, node)

    graph.add_conditional_edges(START, after_start, ["direct", "scope"])
    graph.add_edge("direct", END)
    graph.add_edge("scope", "route")
    graph.add_conditional_edges("route", after_route, ["contextualize", "trace_config"])
    graph.add_edge("contextualize", "trace_config")
    graph.add_conditional_edges("trace_config", after_trace_config, ["small_talk", "retrieve"])
    graph.add_edge("small_talk", END)
    graph.add_conditional_edges("retrieve", after_retrieve, ["budget", END])
    graph.add_conditional_edges("budget", after_budget, ["summarize", "prompt"])
    graph.add_edge("summarize", "prompt")
    graph.add_edge("prompt", END)
    return graph.compile(name="classic")


__all__ = ["build_classic_graph"]
