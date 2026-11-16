"""What the agent did for one request, recorded as it runs, for ``extra.agent_trace``.

When the request asks for a trace (``metadata.include_retrieval_trace``), the
steps of the ``agentic`` graph (``services.chat.agentic``) record into an
``AgentTrace``: each reply of the model and each round of tool calls. A call
that searches the documents records its search in a retrieval trace of its
own (``search``), the kind ``classic`` returns, so a search reads the same in
both modes. The searches of a run share one budget of candidates, as the
searches of one ``classic`` retrieval do, which keeps a long run within the
bounds of one retrieval trace. ``finish`` makes the trace, in the shape of
``core.models.agent_trace``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.chat.agent import call_arguments, result_summary
from core.models.agent_trace import (
    AGENT_TRACE_SCHEMA_VERSION,
    AgentCallTrace,
    AgentReplyTrace,
    AgentToolsTrace,
    AgentTraceV1,
)
from core.retrieval.trace import RetrievalDiagnosticsContext, RetrievalTraceBuilder
from core.tools import ToolCall, ToolResult
from core.tools.passages import shown_sources


@dataclass(frozen=True)
class CallRecord:
    """A call of a round as it ran: what the model asked, what it was answered.

    *refused*: answered without running (the loop guard, the round's cap).
    *search*: the retrieval trace the call's search recorded into.
    """

    call: ToolCall
    result: ToolResult
    refused: bool = False
    duration_seconds: float | None = None
    search: RetrievalTraceBuilder | None = None

    def trace(self, configuration_fingerprint: str) -> AgentCallTrace:
        if self.refused:
            status = "refused"
        else:
            status = "error" if self.result.is_error else "ok"
        searched = self.search is not None and _searched(self.search)
        return AgentCallTrace(
            id=self.call.id,
            tool=self.call.name,
            arguments=call_arguments(self.call),
            status=status,
            summary=result_summary(self.result),
            duration_seconds=self.duration_seconds,
            sources=shown_sources(self.result.content or ""),
            retrieval_trace=(
                self.search.finish(configuration_fingerprint=configuration_fingerprint) if searched else None
            ),
        )


@dataclass(frozen=True)
class _Round:
    round: int
    duration_seconds: float
    calls: list[CallRecord] = field(default_factory=list)

    def trace(self, configuration_fingerprint: str) -> AgentToolsTrace:
        return AgentToolsTrace(
            round=self.round,
            duration_seconds=self.duration_seconds,
            calls=[call.trace(configuration_fingerprint) for call in self.calls],
        )


class AgentTrace:
    """The steps of one agent run, as they ran; ``finish`` makes the trace."""

    def __init__(self, *, request_id: str, profile: str, max_tool_rounds: int, max_prompt_tokens: int | None) -> None:
        self.request_id = request_id
        self._profile = profile
        self._max_tool_rounds = max_tool_rounds
        self._max_prompt_tokens = max_prompt_tokens
        self._diagnostics = RetrievalDiagnosticsContext()
        self._steps: list[AgentReplyTrace | _Round] = []

    def reply(
        self,
        step: str,
        *,
        round: int,
        duration_seconds: float,
        prompt_tokens: int,
        response: dict,
        reason: str | None = None,
    ) -> None:
        """A reply of the model: *step* ``agent``, offered the tools, or ``finalize`` (with its *reason*)."""
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        self._steps.append(
            AgentReplyTrace(
                step=step,
                round=round,
                duration_seconds=duration_seconds,
                prompt_tokens=prompt_tokens,
                finish_reason=choice.get("finish_reason"),
                tool_calls=len(message.get("tool_calls") or []),
                usage=_usage(response),
                reason=reason,
            )
        )

    def search(self, round: int, index: int) -> RetrievalTraceBuilder:
        """The retrieval trace the *index*-th call of a round records its search into."""
        return RetrievalTraceBuilder(
            f"{self.request_id}:round:{round}:call:{index + 1}", None, diagnostics=self._diagnostics
        )

    def tools(self, *, round: int, duration_seconds: float, calls: list[CallRecord]) -> None:
        """A round of tool calls, in the order the model made them."""
        self._steps.append(_Round(round, duration_seconds, list(calls)))

    def finish(self, *, configuration_fingerprint: str, total_seconds: float) -> dict[str, Any]:
        """The trace, ``core.models.agent_trace.AgentTraceV1`` as JSON."""
        steps = [
            step if isinstance(step, AgentReplyTrace) else step.trace(configuration_fingerprint) for step in self._steps
        ]
        trace = AgentTraceV1(
            schema_version=AGENT_TRACE_SCHEMA_VERSION,
            request_id=self.request_id,
            profile=self._profile,
            max_tool_rounds=self._max_tool_rounds,
            max_prompt_tokens=self._max_prompt_tokens,
            rounds=sum(isinstance(step, AgentToolsTrace) for step in steps),
            finalized=any(step.step == "finalize" for step in steps),
            steps=steps,
            timings={"total": total_seconds},
            configuration_fingerprint=configuration_fingerprint,
        )
        return trace.model_dump(mode="json")


def _searched(trace: RetrievalTraceBuilder) -> bool:
    """Whether a search recorded anything into *trace*: a tool given one may not search."""
    return bool(trace.timings or trace.query_traces or trace.errors) or any(
        stage.status != "not_run" for stage in trace.stages.values()
    )


def _usage(response: dict) -> dict[str, int] | None:
    """The token counts the endpoint reported for *response*, the reasoning's among them when it gives one."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = {
        key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if _count(usage.get(key))
    }
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and _count(details.get("reasoning_tokens")):
        counts["reasoning_tokens"] = details["reasoning_tokens"]
    return counts or None


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = ["AgentTrace", "CallRecord"]
