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
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from core.chat.agent import (
    FINALIZE_INSTRUCTION,
    MAX_CALLS_PER_ROUND,
    assistant_turn,
    call_signature,
    leave_out_old_results,
    tool_calls,
    with_instruction,
)
from core.chat.agent_profile import AgentProfile, AgentProfiles
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
    for ``llm_override``), with the profile's temperature when it sent none. *tools_tokens* is
    what the tool definitions take in the prompt; *max_prompt_tokens* what the
    model's window leaves for it, ``None`` when the window is unknown.
    """

    profile: AgentProfile
    llm: LLM
    tools: ToolRegistry
    tool_context: ToolContext
    sampling: dict
    tools_tokens: int = 0
    max_prompt_tokens: int | None = None


class AgentGraph(Protocol):
    """The compiled ``agentic`` graph (``services.chat.graphs.agentic``)."""

    async def run(self, state: AgentState, *, context: AgentContext) -> AgentState: ...


class AgentSteps:
    """The bodies of the ``agentic`` graph's nodes, and the tests its edges make."""

    async def agent(self, state: AgentState, ctx: AgentContext) -> dict:
        """One reply of the model, offered the tools: calls to run, or the answer."""
        response = await ctx.llm.chat(sanitize_messages(state["messages"]), **ctx.sampling, tools=ctx.tools.schemas())
        message = _message(response)
        calls = tool_calls(message)
        update: dict = {"response": response, "calls": calls}
        if calls:
            update["messages"] = [*state["messages"], assistant_turn(message, calls)]
        return update

    async def tools(self, state: AgentState, ctx: AgentContext) -> dict:
        """Run the calls of the model's reply and add their results to the conversation.

        Calls run concurrently. One already made this turn is answered with an
        error instead of running again, as are those past ``MAX_CALLS_PER_ROUND``.
        """
        made = list(state.get("calls_made", []))
        planned: list[ToolCall | ToolResult] = []
        for index, call in enumerate(state.get("calls", [])):
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

        async def answer(item: ToolCall | ToolResult) -> ToolResult:
            return await run_tool(ctx.tools, item, ctx.tool_context) if isinstance(item, ToolCall) else item

        results = await asyncio.gather(*(answer(item) for item in planned))
        rounds = state.get("rounds", 0) + 1
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

    async def finalize(self, state: AgentState, ctx: AgentContext) -> dict:
        """The answer from what the tools found, with no tools offered.

        The oldest tool results are left out until the conversation fits.
        """
        messages = with_instruction(state["messages"], FINALIZE_INSTRUCTION)
        messages = leave_out_old_results(messages, lambda m: self.fits(m, ctx, with_tools=False))
        response = await ctx.llm.chat(sanitize_messages(messages), **ctx.sampling)
        return {"response": response, "messages": messages, "calls": [], "finalized": True}

    def wants_tools(self, state: AgentState) -> bool:
        """Whether the model's reply calls tools, rather than answering."""
        return bool(state.get("calls"))

    def may_call_tools(self, state: AgentState, ctx: AgentContext) -> bool:
        """Whether the model gets another reply with tools: rounds left, and room for the conversation and the tools."""
        return state.get("rounds", 0) < ctx.profile.max_tool_rounds and self.fits(
            state["messages"], ctx, with_tools=True
        )

    @staticmethod
    def fits(messages: list[dict], ctx: AgentContext, *, with_tools: bool) -> bool:
        """Whether *messages*, and the tool definitions *with_tools*, fit the window."""
        if ctx.max_prompt_tokens is None:
            return True
        tokens = messages_tokens(messages) + (ctx.tools_tokens if with_tools else 0)
        return tokens <= ctx.max_prompt_tokens


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

    async def run(
        self,
        *,
        profile: AgentProfile,
        messages: list[dict],
        custom_prompt: str | None,
        scope: Scope,
        llm: LLM,
        payload: dict,
        max_prompt_tokens: int | None = None,
    ) -> AgentRun:
        """Let the agent answer *messages*, the history without its leading system prompt (*custom_prompt*).

        *payload* is the request as the client sent it: its sampling settings
        and ``metadata`` go to every call of the model. Without a temperature,
        the profile's is used.
        """
        template = await self._prompt_service.resolve_prompt(PromptType.AGENT.value, names=[profile.prompt_name])
        now = self._clock()
        system_prompt = template.format(
            current_date=now.strftime(_DATE_FORMAT),
            calendar_anchors=calendar_anchors(now),
            custom_prompt=custom_prompt_block(custom_prompt),
        )
        sampling = sampling_params(payload)
        if sampling.get("temperature") is None and profile.temperature is not None:
            sampling["temperature"] = profile.temperature
        tools = self._tools[profile.name]
        tool_context = ToolContext(scope=scope)
        context = AgentContext(
            profile=profile,
            llm=llm,
            tools=tools,
            tool_context=tool_context,
            sampling=sampling,
            tools_tokens=tool_definition_tokens({"tools": tools.schemas()}, get_num_tokens()),
            max_prompt_tokens=max_prompt_tokens,
        )
        state = await self._graph.run(
            {"messages": [{"role": "system", "content": system_prompt}, *messages], "rounds": 0, "calls_made": []},
            context=context,
        )
        run = AgentRun(
            response=_without_tool_calls(state["response"]),
            messages=state["messages"],
            sources=tool_context.sources.entries,
            rounds=state.get("rounds", 0),
            finalized=state.get("finalized", False),
        )
        logger.bind(profile=profile.name, rounds=run.rounds, finalized=run.finalized, sources=len(run.sources)).info(
            "Agent answered"
        )
        return run


def _message(response: dict) -> dict:
    choices = response.get("choices") or [{}]
    return choices[0].get("message") or {}


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


__all__ = ["AgentContext", "AgentGraph", "AgentRun", "AgentSteps", "AgenticChat"]
