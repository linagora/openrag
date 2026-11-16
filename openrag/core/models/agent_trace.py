"""Typed contract for what the agent did for one request, returned in ``extra.agent_trace``.

An ``agentic`` request with ``metadata.include_retrieval_trace`` gets it, as a
``classic`` one gets ``extra.retrieval_trace``. It has one entry per step of
the graph, in the order they ran: each reply of the model (``agent``, offered
the tools; ``finalize``, without them) and each round of tool calls
(``tools``). Like the retrieval trace it holds no document content: a call
shows what the model asked (its arguments, its queries), the first line of
what it was answered and the ``[Source N]`` numbers it was shown; a call that
searched carries the retrieval trace of its search (schema version 1).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AGENT_TRACE_SCHEMA_VERSION = 1

#: Why ``finalize`` answered without tools.
FinalizeReason = Literal["no rounds left", "window full", "recursion limit"]
#: ``refused``: answered without running, made already this turn or past the round's cap.
CallStatus = Literal["ok", "error", "refused"]


class _TraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentReplyTrace(_TraceModel):
    """One reply of the model."""

    step: Literal["agent", "finalize"]
    round: int = Field(ge=1)
    duration_seconds: float = Field(ge=0)
    #: The prompt as OpenRAG counts it against the window: the conversation,
    #: and the tools' definitions when they are offered.
    prompt_tokens: int = Field(ge=0)
    finish_reason: str | None = None
    #: The tool calls the reply made; 0 for an answer.
    tool_calls: int = Field(default=0, ge=0)
    #: The token counts the endpoint reported, when it reported them.
    usage: dict[str, int] | None = None
    #: ``finalize`` only: why the model answered without tools.
    reason: FinalizeReason | None = None


class AgentCallTrace(_TraceModel):
    """One tool call of a round."""

    id: str
    tool: str
    #: The arguments as the model sent them: parsed JSON, or the raw text when it doesn't parse.
    arguments: Any
    status: CallStatus
    #: The first line of what the model was answered.
    summary: str
    #: ``None`` for a call that did not run.
    duration_seconds: float | None = Field(default=None, ge=0)
    #: The ``[Source N]`` numbers the answer showed passages under, in its order.
    sources: list[int] = Field(default_factory=list)
    #: The retrieval trace of the call's search (schema version 1), when it searched.
    retrieval_trace: dict[str, Any] | None = None


class AgentToolsTrace(_TraceModel):
    """One round of tool calls, run at once."""

    step: Literal["tools"] = "tools"
    round: int = Field(ge=1)
    duration_seconds: float = Field(ge=0)
    calls: list[AgentCallTrace]


class AgentTraceV1(_TraceModel):
    """Version 1 of the agent's trace."""

    schema_version: Literal[1]
    request_id: str
    profile: str
    max_tool_rounds: int
    #: What the model's window leaves for the prompt; ``None`` when the window is unknown.
    max_prompt_tokens: int | None
    #: The rounds of tool calls that ran.
    rounds: int
    #: The answer came from ``finalize``.
    finalized: bool
    steps: list[AgentReplyTrace | AgentToolsTrace]
    timings: dict[str, float]
    configuration_fingerprint: str


__all__ = [
    "AGENT_TRACE_SCHEMA_VERSION",
    "AgentCallTrace",
    "AgentReplyTrace",
    "AgentToolsTrace",
    "AgentTraceV1",
    "CallStatus",
    "FinalizeReason",
]
