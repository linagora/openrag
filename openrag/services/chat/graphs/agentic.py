"""The ``agentic`` graph: the agent's loop and its two ends.

The only code besides ``graphs.classic`` that sees LangGraph. Each node
unpacks the request's context and calls its step in ``services.chat.agentic``;
the edges decide what comes next.

    START ─┬─ room and rounds left ──→ agent ─┬─ tool calls ──→ tools ─┐
           │                            ▲     └─ answer ──→ END       │
           │                            └── room and rounds left ─────┤
           └─ otherwise ──────────────────────────→ finalize ←────────┘
                                                        ▼
                                                       END

``finalize`` is the soft limit: the model answers from what it found, with no
tools offered. LangGraph's recursion limit is the hard one, set a few steps
past what the profile allows; should it be reached anyway, the run still ends
with ``finalize``'s answer, never an error.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from core.chat.state import AgentState
from core.utils.logging import get_logger
from services.chat.agentic import AgentContext, AgentSteps

# See graphs.classic: the import's pending-deprecation warning about the
# checkpoint serializer is dropped, any other is raised again.
with warnings.catch_warnings(record=True) as _import_warnings:
    from langgraph.errors import GraphRecursionError
    from langgraph.graph import END, START, StateGraph
    from langgraph.runtime import Runtime
for _warning in _import_warnings:
    if not (issubclass(_warning.category, PendingDeprecationWarning) and "allowed_objects" in str(_warning.message)):
        warnings.warn_explicit(_warning.message, _warning.category, _warning.filename, _warning.lineno)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = get_logger()

#: Steps allowed past two per round (agent, tools): the last reply or ``finalize``, and a margin.
_SPARE_STEPS = 4


class CompiledAgent:
    """The compiled ``agentic`` graph, run with the hard limit and its fallback."""

    def __init__(self, graph: CompiledStateGraph, steps: AgentSteps) -> None:
        self._graph = graph
        self._steps = steps

    async def run(self, state: AgentState, *, context: AgentContext) -> AgentState:
        """Run the agent from *state* until it answers."""
        last: AgentState = state
        try:
            async for values in self._graph.astream(
                state,
                context=context,
                stream_mode="values",
                config={"recursion_limit": 2 * context.profile.max_tool_rounds + _SPARE_STEPS},
            ):
                last = values
        except GraphRecursionError:
            logger.bind(profile=context.profile.name, rounds=last.get("rounds", 0)).warning(
                "Agent reached the graph's recursion limit; answering from what it found"
            )
            last = {**last, **await self._steps.finalize(last, context)}
        return last


def build_agent_graph(steps: AgentSteps) -> CompiledAgent:
    """Compile the ``agentic`` graph over *steps*; compiled once, run for every agentic request."""

    async def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        return await steps.agent(state, runtime.context)

    async def tools(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        return await steps.tools(state, runtime.context)

    async def finalize(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        return await steps.finalize(state, runtime.context)

    def next_reply(state: AgentState, runtime: Runtime[AgentContext]) -> str:
        return "agent" if steps.may_call_tools(state, runtime.context) else "finalize"

    def after_agent(state: AgentState) -> str:
        return "tools" if steps.wants_tools(state) else END

    graph = StateGraph(AgentState, context_schema=AgentContext)
    for node in (agent, tools, finalize):
        graph.add_node(node.__name__, node)

    graph.add_conditional_edges(START, next_reply, ["agent", "finalize"])
    graph.add_conditional_edges("agent", after_agent, ["tools", END])
    graph.add_conditional_edges("tools", next_reply, ["agent", "finalize"])
    graph.add_edge("finalize", END)
    return CompiledAgent(graph.compile(name="agentic"), steps)


__all__ = ["CompiledAgent", "build_agent_graph"]
