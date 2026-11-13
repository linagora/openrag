"""The state the chat graphs carry from node to node.

Each key is written by one node and read by the ones after it; a node returns
only the keys it changes. Per-request data that no node writes (the options,
the scope, the LLM, the trace) is not here: it travels in the graph's context
(``services.chat.classic.ChatContext``).
"""

from __future__ import annotations

from typing import Any, TypedDict

# Imported at run time, not only for type checkers: LangGraph reads the hints.
from core.chat.smalltalk import CasualMessagePolicy
from core.models.chunk import Chunk
from core.models.query import SearchQueries
from core.tools import ToolCall
from core.websearch import WebResult


class ClassicState(TypedDict, total=False):
    """What the ``classic`` graph knows about a request at each step."""

    # Input: the history window without its leading system prompt, which is
    # the client's own (``custom_prompt``), and the user's last message.
    messages: list[dict]
    custom_prompt: str | None
    last_user_message: str

    # route, then contextualize, which can recognise an acknowledgement.
    casual: CasualMessagePolicy | None
    acknowledgement_candidate: bool
    retrieval_forced: bool
    queries: SearchQueries | None

    # trace_config: set only when a trace is recorded.
    resolved_plan: Any
    configuration_fingerprint: str

    # retrieve
    chunks: list[Chunk]
    web_results: list[WebResult]

    # budget: the history as the model gets it, the answer template, the room
    # the window leaves for sources (``None`` when the window is unknown).
    history: list[dict]
    template: str
    current_date: str
    room: int | None
    tools_tokens: int
    # The full retrieval set, before map-reduce and the fit.
    retrieved_docs: list
    retrieved_web_results: list[WebResult]

    # budget, summarize, prompt: the sources as they reach the prompt.
    docs: list

    # small_talk or prompt: the messages to send, and how to answer them.
    answer_messages: list[dict]
    citation_protocol_active: bool
    retry_empty_response: bool


class AgentState(TypedDict, total=False):
    """What the ``agentic`` graph knows about a request at each step."""

    # The conversation the model sees: its prompt, the history, then each
    # round's calls and their results.
    messages: list[dict]
    # The model's last reply, a chat completion: tool calls, or the answer.
    response: dict
    # The calls of that reply, which the ``tools`` node runs.
    calls: list[ToolCall]
    # Rounds of tool calls run so far.
    rounds: int
    # The calls already made this turn, for the loop guard
    # (``core.chat.agent.call_signature``).
    calls_made: list[str]
    # The answer came from ``finalize``: the rounds ran out or the window filled.
    finalized: bool


__all__ = ["AgentState", "ClassicState"]
