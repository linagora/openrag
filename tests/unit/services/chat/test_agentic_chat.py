"""Unit tests for ``services.chat.agentic.AgenticChat``: preparing a request for the agent and reading its run.

The loop itself is tested in ``graphs/test_agentic_graph.py``; here the graph
is the real one and the model is scripted.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from core.chat.scope import Scope
from core.config.agentic import AgenticConfig, AgentProfileConfig
from core.models.chunk import Chunk
from core.tools import ToolContext, ToolRegistry, tool
from core.utils.exceptions import ConfigError, ValidationError
from services.chat.agentic import AgenticChat, AgentRequest, AgentSteps
from services.chat.graphs.agentic import build_agent_graph

_NOW = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


class Toolset:
    @tool("lookup", "Look a word up.")
    async def lookup(self, ctx: ToolContext, word: str) -> str:
        number = ctx.sources.number(Chunk(id=word, text=f"about {word}", metadata={}), key=word)
        return f"[Source {number}] about {word}"

    @tool("find_files", "Find files.")
    async def find_files(self, ctx: ToolContext, name: str) -> str:
        return "No file matched."


class Prompts:
    def __init__(self) -> None:
        self.asked: list[tuple[str, list]] = []

    async def resolve_prompt(self, prompt_type, names=None):
        self.asked.append((prompt_type, list(names or [])))
        return "Today: {current_date}\n{calendar_anchors}\n{custom_prompt}--"


class LLM:
    def __init__(self, *replies) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        return copy.deepcopy(self._replies.pop(0))


def _reply(content=None, **calls) -> dict:
    tool_calls = [
        {"id": f"call{i:05d}", "type": "function", "function": {"name": name, "arguments": arguments}}
        for i, (name, arguments) in enumerate(calls.items())
    ]
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}


def _agentic(profiles=None, prompts=None) -> AgenticChat:
    config = SimpleNamespace(
        agentic=AgenticConfig(profiles=profiles or {"default": AgentProfileConfig(tools=["lookup", "find_files"])})
    )
    return AgenticChat(
        config=config,
        prompt_service=prompts or Prompts(),
        registry=ToolRegistry.from_toolsets(Toolset()),
        graph=build_agent_graph(AgentSteps()),
        clock=lambda: _NOW,
    )


_PAYLOAD = {
    "model": "openrag-p",
    "stream": False,
    "messages": [],
    "temperature": 0.7,
    "metadata": {"llm_override": {"model": "m"}},
}


def _request(agentic: AgenticChat, llm, *, profile=None, payload=None, custom_prompt=None) -> AgentRequest:
    return AgentRequest(
        profile=agentic.profile(profile),
        messages=[{"role": "user", "content": "q"}],
        custom_prompt=custom_prompt,
        scope=Scope(partitions=["p"]),
        llm=llm,
        payload=payload or _PAYLOAD,
    )


async def _run(agentic: AgenticChat, llm, **request):
    return await agentic.run(_request(agentic, llm, **request))


@pytest.mark.asyncio
async def test_the_agent_gets_its_prompt_with_the_date_the_calendar_and_the_clients_instruction():
    prompts = Prompts()
    llm = LLM(_reply("Hi. [Sources: none]"))

    await _run(_agentic(prompts=prompts), llm, custom_prompt="Answer in French")

    assert prompts.asked == [("agent", [None])]
    system = llm.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("Today: Friday, November 13, 2026, 09:30:00\nCalendar anchors")
    assert "- last week [2026-11-02, 2026-11-09)" in system["content"]
    assert "<unsafe_custom_prompt>\nAnswer in French\n</unsafe_custom_prompt>" in system["content"]
    assert llm.calls[0]["messages"][1:] == [{"role": "user", "content": "q"}]


@pytest.mark.asyncio
async def test_the_profile_picks_the_prompt_and_the_tools():
    profiles = {
        "default": AgentProfileConfig(tools=["lookup"]),
        "files": AgentProfileConfig(prompt_name="librarian", tools=["find_files"]),
    }
    prompts = Prompts()
    llm = LLM(_reply("ok"), _reply("ok"))
    agentic = _agentic(profiles, prompts)

    await _run(agentic, llm)
    await _run(agentic, llm, profile="files")

    default_call, files_call = llm.calls
    assert [t["function"]["name"] for t in default_call["tools"]] == ["lookup"]
    assert [t["function"]["name"] for t in files_call["tools"]] == ["find_files"]
    assert prompts.asked == [("agent", [None]), ("agent", ["librarian"])]
    assert files_call["metadata"] == {"llm_override": {"model": "m"}}, "llm_override reaches every call"
    assert "stream" not in files_call and "model" not in files_call, "the transport keys are not sampling"


@pytest.mark.parametrize(
    ("request_temperature", "profile_temperature", "used"),
    [
        (0.7, 0.2, 0.7),  # the request's, when it sends one
        (None, 0.2, 0.2),  # the profile's otherwise
        (None, None, None),  # neither: the endpoint's own
    ],
)
@pytest.mark.asyncio
async def test_the_request_s_temperature_wins_and_the_profile_s_fills_in(
    request_temperature, profile_temperature, used
):
    profiles = {"default": AgentProfileConfig(tools=["lookup"], temperature=profile_temperature)}
    payload = {key: value for key, value in _PAYLOAD.items() if key != "temperature"}
    if request_temperature is not None:
        payload["temperature"] = request_temperature
    llm = LLM(_reply("ok"))

    await _run(_agentic(profiles), llm, payload=payload)

    assert llm.calls[0].get("temperature") == used


@pytest.mark.asyncio
async def test_the_run_returns_the_answer_and_every_passage_the_tools_showed():
    llm = LLM(_reply(lookup='{"word": "acme"}'), _reply("ACME. [Sources: 1]"))

    run = await _run(_agentic(), llm)

    assert run.response["choices"][0]["message"]["content"] == "ACME. [Sources: 1]"
    assert [chunk.id for chunk in run.sources] == ["acme"]
    assert run.rounds == 1 and run.finalized is False
    assert [m["role"] for m in run.messages] == ["system", "user", "assistant", "tool"]


@pytest.mark.asyncio
async def test_tool_calls_in_the_answer_never_reach_the_client():
    # A model may still emit calls when none are offered (finalize): they are the agent's, not the client's.
    profiles = {"default": AgentProfileConfig(tools=["lookup"], max_tool_rounds=1)}
    llm = LLM(_reply(lookup='{"word": "acme"}'), _reply("", lookup='{"word": "orange"}'))

    run = await _run(_agentic(profiles), llm)

    assert run.finalized is True
    assert "tool_calls" not in run.response["choices"][0]["message"]
    assert run.response["choices"][0]["finish_reason"] == "stop", "so an empty answer is retried, as any other"


def test_an_unknown_profile_is_a_400():
    with pytest.raises(ValidationError) as error:
        _agentic().profile("lawyer")

    assert error.value.status_code == 400


def test_a_profile_naming_a_tool_that_does_not_exist_fails_at_startup():
    with pytest.raises(ConfigError, match="Agent profile 'default': Unknown tools: web_search. The tools are: lookup"):
        _agentic({"default": AgentProfileConfig(tools=["lookup", "web_search"])})
