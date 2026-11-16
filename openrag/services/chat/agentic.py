"""The ``agentic`` mode: what each node of its graph does, and running the agent for a chat request.

The graph (``services.chat.graphs.agentic``, the only module importing
LangGraph) loops: the model is offered the profile's tools; when it calls
some, ``tools`` runs them and the model reads their results; when it answers,
that is the answer. When the profile's rounds run out or the conversation no
longer fits the window, ``finalize`` asks for the answer once more, with no
tools offered. The steps here are plain async methods, testable without
LangGraph.

``AgenticChat`` prepares a request for the graph: the profile, its prompt (a
prompt of type ``agent``, with the date and the calendar anchors for the
tools' date arguments), the tools it offers, and the ``ToolContext`` holding
the request's scope and the ``[Source N]`` numbers of the passages the tools
show. The answer is the model's last reply; ``AnswerService`` then shapes it
like any other, citations included.

When the request streams, every reply of the model is streamed
(``AgentStream``): its thinking goes to the client as it comes, its text once
it is clear no tool call follows (the sentence a model writes before a call is
shown as thinking), and its tool calls are put back together and run. Each
call is announced when it starts and when it ends (``step_line``), so a client
can show the agent at work before the answer comes.

When the request asks for a trace, the steps record what they do into the
context's ``AgentTrace`` (``core.chat.agent_trace``): each reply of the model,
each round of calls, and each call's search in a retrieval trace of its own.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from core.chat.agent import (
    FINALIZE_INSTRUCTION,
    MAX_CALLS_PER_ROUND,
    ReplyLines,
    assistant_turn,
    call_arguments,
    call_signature,
    finish_line,
    leave_out_old_results,
    result_summary,
    sse_chunk,
    step_line,
    tool_calls,
)
from core.chat.agent_profile import AgentProfile, AgentProfiles
from core.chat.agent_trace import CallRecord
from core.chat.answer import sampling_params
from core.chat.history import sanitize_messages
from core.models.prompt import PromptType
from core.prompts import calendar_anchors, custom_prompt_block, tool_definition_tokens
from core.tools import ToolCall, ToolContext, ToolRegistry, ToolResult, run_tool
from core.utils.exceptions import ConfigError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from services.chat.classic import messages_tokens

if TYPE_CHECKING:
    from core.chat.agent_trace import AgentTrace
    from core.chat.scope import Scope
    from core.chat.state import AgentState
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.models.chunk import Chunk
    from services.orchestrators.prompt_service import PromptService

logger = get_logger()

_DATE_FORMAT = "%A, %B %d, %Y, %H:%M:%S"


@dataclass(frozen=True)
class AgentContext:
    """What the agent runs with besides its state, settled before the graph starts.

    *sampling* is what the request asked of the model (its ``metadata`` too,
    for ``llm_override``), with the profile's temperature and reasoning_effort
    when it sent none.
    *tools_tokens* is what the tool definitions take in the prompt;
    *max_prompt_tokens* what the model's window leaves for it, ``None`` when
    the window is unknown. With *stream*, the model's replies are streamed.
    With *trace*, the steps record what they do into it.
    """

    profile: AgentProfile
    llm: LLM
    tools: ToolRegistry
    tool_context: ToolContext
    sampling: dict
    tools_tokens: int = 0
    max_prompt_tokens: int | None = None
    stream: bool = False
    trace: AgentTrace | None = None


#: Where a step writes the SSE lines the client gets as the agent works.
Emit = Callable[[str], None]


class AgentGraph(Protocol):
    """The compiled ``agentic`` graph (``services.chat.graphs.agentic``)."""

    async def run(self, state: AgentState, *, context: AgentContext) -> AgentState: ...

    def stream(self, state: AgentState, *, context: AgentContext) -> AsyncIterator[tuple[str, Any]]:
        """``("custom", line)`` for each SSE line the steps write, ``("values", state)`` after each step."""
        ...


class AgentSteps:
    """The bodies of the ``agentic`` graph's nodes, and the tests its edges make.

    *emit* is where the steps write the client's SSE lines when the request
    streams: ``agent`` and ``finalize`` the model's thinking and text,
    ``tools`` each call's start and end.
    """

    async def agent(self, state: AgentState, ctx: AgentContext, emit: Emit | None = None) -> dict:
        """One reply of the model, offered the tools: calls to run, or the answer."""
        started = time.perf_counter()
        response = await self._reply(state["messages"], ctx, with_tools=True, emit=emit)
        if ctx.trace is not None:
            ctx.trace.reply(
                "agent",
                round=state.get("rounds", 0) + 1,
                duration_seconds=time.perf_counter() - started,
                prompt_tokens=self.prompt_tokens(state["messages"], ctx, with_tools=True),
                response=response,
            )
        message = _message(response)
        calls = tool_calls(message)
        if calls:
            return {
                "response": response,
                "calls": calls,
                "messages": [*state["messages"], assistant_turn(message, calls)],
            }
        if ctx.stream and emit is not None:
            emit(finish_line(response))
        return {"response": response, "calls": []}

    async def tools(self, state: AgentState, ctx: AgentContext, emit: Emit | None = None) -> dict:
        """Run the calls of the model's reply and add their results to the conversation.

        Calls run concurrently. One already made this turn is answered with an
        error instead of running again, as are those past ``MAX_CALLS_PER_ROUND``.
        Streamed, each call is announced as it starts and as it ends. Traced,
        each call that runs gets a retrieval trace of its own for its search.
        """
        started = time.perf_counter()
        calls = state.get("calls", [])
        made = list(state.get("calls_made", []))
        planned: list[ToolCall | ToolResult] = []
        for index, call in enumerate(calls):
            signature = call_signature(call)
            if index >= MAX_CALLS_PER_ROUND:
                planned.append(
                    _refused(
                        call,
                        f"only {MAX_CALLS_PER_ROUND} calls run in one round. Make this one again if you still need it.",
                    )
                )
            elif signature in made:
                planned.append(_refused(call, "you already made this call: its result is above."))
            else:
                made.append(signature)
                planned.append(call)

        round_number = state.get("rounds", 0) + 1
        announce = emit if ctx.stream else None
        if announce is not None:
            for call in calls:
                announce(step_line(_call_started(call, round_number)))
        trace = ctx.trace

        async def answer(index: int, item: ToolCall | ToolResult) -> CallRecord:
            if isinstance(item, ToolResult):
                record = CallRecord(calls[index], item, refused=True)
            else:
                search = trace.search(round_number, index) if trace is not None else None
                tool_context = replace(ctx.tool_context, trace=search) if search is not None else ctx.tool_context
                call_started = time.perf_counter()
                result = await run_tool(ctx.tools, item, tool_context)
                record = CallRecord(item, result, duration_seconds=time.perf_counter() - call_started, search=search)
            if announce is not None:
                announce(step_line(_call_ended(record.result, round_number)))
            return record

        records = await asyncio.gather(*(answer(index, item) for index, item in enumerate(planned)))
        results = [record.result for record in records]
        if trace is not None:
            trace.tools(round=round_number, duration_seconds=time.perf_counter() - started, calls=records)
        rounds = round_number
        logger.bind(
            profile=ctx.profile.name,
            round=rounds,
            tools=[result.name for result in results],
            errors=sum(result.is_error for result in results),
        ).debug("Agent tool round")
        return {
            "messages": [*state["messages"], *(result.as_message() for result in results)],
            "rounds": rounds,
            "calls_made": made,
            "calls": [],
        }

    async def finalize(
        self, state: AgentState, ctx: AgentContext, emit: Emit | None = None, *, reason: str | None = None
    ) -> dict:
        """The answer from what the tools found, with no tools offered.

        The model is told so after the tool results, where it reads it last;
        the oldest results are left out until the conversation fits. *reason*
        is why, for the trace: ``why_finalize``'s when not given.
        """
        started = time.perf_counter()
        messages = [*state["messages"], {"role": "user", "content": FINALIZE_INSTRUCTION}]
        messages = leave_out_old_results(messages, lambda m: self.fits(m, ctx, with_tools=False))
        response = await self._reply(messages, ctx, with_tools=False, emit=emit)
        if ctx.trace is not None:
            ctx.trace.reply(
                "finalize",
                round=state.get("rounds", 0) + 1,
                duration_seconds=time.perf_counter() - started,
                prompt_tokens=self.prompt_tokens(messages, ctx, with_tools=False),
                response=response,
                reason=reason or self.why_finalize(state, ctx),
            )
        if ctx.stream and emit is not None:
            emit(finish_line(response))
        return {"response": response, "messages": messages, "calls": [], "finalized": True}

    async def _reply(self, messages: list[dict], ctx: AgentContext, *, with_tools: bool, emit: Emit | None) -> dict:
        """The model's reply to *messages*, as a chat completion.

        When the request streams, the reply is streamed: its text is written
        to *emit* as it comes, and the whole reply put back together.
        """
        kwargs = {**ctx.sampling, "tools": ctx.tools.schemas()} if with_tools else dict(ctx.sampling)
        messages = sanitize_messages(messages)
        if not ctx.stream or emit is None:
            return await ctx.llm.chat(messages, **kwargs)
        lines = ReplyLines()
        async for line in ctx.llm.stream_chat(messages, **kwargs):
            chunk = sse_chunk(line)
            if chunk is not None:
                for out in lines.add(chunk):
                    emit(out)
        for out in lines.end():
            emit(out)
        return lines.reply.response()

    def wants_tools(self, state: AgentState) -> bool:
        """Whether the model's reply calls tools, rather than answering."""
        return bool(state.get("calls"))

    def may_call_tools(self, state: AgentState, ctx: AgentContext) -> bool:
        """Whether the model gets another reply with tools: rounds left, and room for the conversation and the tools."""
        return state.get("rounds", 0) < ctx.profile.max_tool_rounds and self.fits(
            state["messages"], ctx, with_tools=True
        )

    def why_finalize(self, state: AgentState, ctx: AgentContext) -> str:
        """Why the model answers without tools: its rounds ran out, or the conversation outgrew the window."""
        return "no rounds left" if state.get("rounds", 0) >= ctx.profile.max_tool_rounds else "window full"

    @staticmethod
    def prompt_tokens(messages: list[dict], ctx: AgentContext, *, with_tools: bool) -> int:
        """What *messages*, and the tool definitions *with_tools*, take of the window."""
        return messages_tokens(messages) + (ctx.tools_tokens if with_tools else 0)

    @classmethod
    def fits(cls, messages: list[dict], ctx: AgentContext, *, with_tools: bool) -> bool:
        """Whether *messages*, and the tool definitions *with_tools*, fit the window."""
        if ctx.max_prompt_tokens is None:
            return True
        return cls.prompt_tokens(messages, ctx, with_tools=with_tools) <= ctx.max_prompt_tokens


@dataclass(frozen=True)
class AgentRequest:
    """A chat request as the agent takes it.

    *messages* is the history without its leading system prompt, which is
    the client's own instruction (*custom_prompt*). *payload* is the request
    as the client sent it: its sampling settings and ``metadata`` go to every
    call of the model; without a temperature, the profile's is used. With
    *trace*, what the agent does is recorded into it.
    """

    profile: AgentProfile
    messages: list[dict]
    custom_prompt: str | None
    scope: Scope
    llm: LLM
    payload: dict
    max_prompt_tokens: int | None = None
    trace: AgentTrace | None = None


@dataclass(frozen=True)
class AgentRun:
    """What the agent did for a request."""

    #: The answer, a chat completion with no tool calls.
    response: dict
    #: The conversation the model last saw.
    messages: list[dict]
    #: Every passage the tools showed, in ``[Source N]`` order.
    sources: list[Chunk]
    rounds: int
    finalized: bool

    @classmethod
    def of(cls, state: AgentState, ctx: AgentContext) -> AgentRun:
        run = cls(
            response=_without_tool_calls(state["response"]),
            messages=state["messages"],
            sources=ctx.tool_context.sources.entries,
            rounds=state.get("rounds", 0),
            finalized=state.get("finalized", False),
        )
        logger.bind(
            profile=ctx.profile.name, rounds=run.rounds, finalized=run.finalized, sources=len(run.sources)
        ).info("Agent answered")
        return run


class AgentStream:
    """The agent's run for a streaming request: the answer's SSE lines as the model writes them, then ``[DONE]``.

    The tools run before the answer's text comes, so ``sources`` is complete
    by then; it grows while they run. ``run`` is set once the lines are out.
    """

    def __init__(self, chunks: AsyncIterator[tuple[str, Any]], ctx: AgentContext) -> None:
        self._chunks = chunks
        self._ctx = ctx
        self.run: AgentRun | None = None

    @property
    def sources(self) -> list[Chunk]:
        """Every passage the tools have shown so far, in ``[Source N]`` order."""
        return self._ctx.tool_context.sources.entries

    def __aiter__(self) -> AsyncIterator[str]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[str]:
        state: AgentState | None = None
        async with aclosing(self._chunks) as chunks:
            async for mode, value in chunks:
                if mode == "custom":
                    yield value
                else:
                    state = value
        if state is not None and "response" in state:
            self.run = AgentRun.of(state, self._ctx)
        yield "data: [DONE]"


class AgenticChat:
    """Runs the agent for a chat request: its profile, its prompt, its tools, the graph.

    Each profile's tools are taken from *registry* here, so a profile naming
    a tool that doesn't exist fails when the service is built, not on a request.
    """

    def __init__(
        self,
        *,
        config: Settings,
        prompt_service: PromptService,
        registry: ToolRegistry,
        graph: AgentGraph,
        clock: Callable[[], datetime],
    ) -> None:
        self._profiles = AgentProfiles(config.agentic)
        self._tools: dict[str, ToolRegistry] = {}
        for profile in self._profiles:
            try:
                self._tools[profile.name] = registry.subset(profile.tools)
            except KeyError as e:
                raise ConfigError(
                    f"Agent profile '{profile.name}': {e.args[0]}. The tools are: {', '.join(registry.names)}."
                ) from None
        self._prompt_service = prompt_service
        self._graph = graph
        self._clock = clock

    def profile(self, name: str | None) -> AgentProfile:
        """The profile named *name*, the default for ``None``; an unknown name is a 400."""
        return self._profiles.get(name)

    async def run(self, request: AgentRequest) -> AgentRun:
        """Let the agent answer *request*."""
        state, ctx = await self._start(request, stream=False)
        return AgentRun.of(await self._graph.run(state, context=ctx), ctx)

    async def stream(self, request: AgentRequest) -> AgentStream:
        """Let the agent answer *request*, streaming its answer."""
        state, ctx = await self._start(request, stream=True)
        return AgentStream(self._graph.stream(state, context=ctx), ctx)

    async def _start(self, request: AgentRequest, *, stream: bool) -> tuple[AgentState, AgentContext]:
        """The agent's first state, and the context it runs with."""
        profile = request.profile
        template = await self._prompt_service.resolve_prompt(PromptType.AGENT.value, names=[profile.prompt_name])
        now = self._clock()
        system_prompt = template.format(
            current_date=now.strftime(_DATE_FORMAT),
            calendar_anchors=calendar_anchors(now),
            custom_prompt=custom_prompt_block(request.custom_prompt),
        )
        sampling = sampling_params(request.payload)
        if sampling.get("temperature") is None and profile.temperature is not None:
            sampling["temperature"] = profile.temperature
        if sampling.get("reasoning_effort") is None and profile.reasoning_effort is not None:
            sampling["reasoning_effort"] = profile.reasoning_effort
        tools = self._tools[profile.name]
        tool_context = ToolContext(scope=request.scope)
        context = AgentContext(
            profile=profile,
            llm=request.llm,
            tools=tools,
            tool_context=tool_context,
            sampling=sampling,
            tools_tokens=tool_definition_tokens({"tools": tools.schemas()}, get_num_tokens()),
            max_prompt_tokens=request.max_prompt_tokens,
            stream=stream,
            trace=request.trace,
        )
        state: AgentState = {
            "messages": [{"role": "system", "content": system_prompt}, *request.messages],
            "rounds": 0,
            "calls_made": [],
        }
        return state, context


def _message(response: dict) -> dict:
    choices = response.get("choices") or [{}]
    return choices[0].get("message") or {}


def _call_started(call: ToolCall, round_number: int) -> dict:
    """The event a client gets when *call* starts: the tool and what the model asked of it."""
    return {
        "type": "tool_call",
        "round": round_number,
        "id": call.id,
        "tool": call.name,
        "arguments": call_arguments(call),
    }


def _call_ended(result: ToolResult, round_number: int) -> dict:
    """The event a client gets when a call ends: whether it failed, and the first line of what it returned."""
    return {
        "type": "tool_result",
        "round": round_number,
        "id": result.call_id,
        "tool": result.name,
        "status": "error" if result.is_error else "ok",
        "summary": result_summary(result),
    }


def _refused(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult(call.id, call.name, f"Error: {reason}", is_error=True)


def _without_tool_calls(response: dict) -> dict:
    """*response* without tool calls: a model may still emit some when none were offered, and they are ours.

    Its finish reason becomes ``stop``: the reply is the answer, and an empty
    one is then retried like any other.
    """
    message = _message(response)
    called = any(message.pop(key, None) for key in ("tool_calls", "function_call"))
    choice = (response.get("choices") or [{}])[0]
    if called or choice.get("finish_reason") in ("tool_calls", "function_call"):
        choice["finish_reason"] = "stop"
    return response


__all__ = [
    "AgentContext",
    "AgentGraph",
    "AgentRequest",
    "AgentRun",
    "AgentSteps",
    "AgentStream",
    "AgenticChat",
    "Emit",
]
