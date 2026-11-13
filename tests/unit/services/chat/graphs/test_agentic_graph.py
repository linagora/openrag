"""Unit tests for the ``agentic`` graph (``services.chat.graphs.agentic``) and its steps (``services.chat.agentic``).

The compiled graph runs with the real ``AgentSteps``, a scripted model and a
toy tool, so each test reads as one turn of the agent: what the model was
offered, which calls ran, how the turn ended.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from core.chat.agent import FINALIZE_INSTRUCTION, LEFT_OUT_RESULT, MAX_CALLS_PER_ROUND
from core.chat.agent_profile import AgentProfile
from core.chat.scope import Scope
from core.models.chunk import Chunk
from core.tools import ToolContext, ToolRegistry, tool
from services.chat.agentic import AgentContext, AgentSteps
from services.chat.graphs.agentic import build_agent_graph


class Lookup:
    """One tool, ``lookup(word)``: shows a passage about the word as ``[Source N]``."""

    def __init__(self, *, text: str = "about {word}") -> None:
        self.words: list[str] = []
        self._text = text

    @tool("lookup", "Look a word up.")
    async def lookup(self, ctx: ToolContext, word: str) -> str:
        self.words.append(word)
        number = ctx.sources.number(Chunk(id=word, text=self._text.format(word=word), metadata={}), key=word)
        return f"[Source {number}] {self._text.format(word=word)}"


def _calls(*words: str) -> dict:
    """A reply calling ``lookup`` once per word."""
    tool_calls = [
        {"id": f"call{i:05d}", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"word": w})}}
        for i, w in enumerate(words)
    ]
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]}


def _answer(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class ScriptedLLM:
    """Replies in order; a callable reply is called for each request. Records what each call was sent."""

    def __init__(self, *replies) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        reply = self._replies.pop(0) if self._replies else _answer("done [Sources: none]")
        return reply() if callable(reply) else copy.deepcopy(reply)


class Tireless(ScriptedLLM):
    """Looks a new word up whenever it is offered tools, and answers only when it isn't."""

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        if "tools" in kwargs:
            return _calls(f"w{len(self.calls)}")
        return _answer("What I found. [Sources: 1]")


def _context(llm, toolset=None, *, max_tool_rounds=3, max_prompt_tokens=None, sampling=None) -> AgentContext:
    registry = ToolRegistry.from_toolsets(toolset or Lookup())
    return AgentContext(
        profile=AgentProfile("default", None, tuple(registry.names), None, max_tool_rounds),
        llm=llm,
        tools=registry,
        tool_context=ToolContext(scope=Scope(partitions=["p"])),
        sampling=sampling or {},
        max_prompt_tokens=max_prompt_tokens,
    )


_START = {"messages": [{"role": "system", "content": "Rules."}, {"role": "user", "content": "q"}], "rounds": 0}


async def _run(ctx: AgentContext, steps: AgentSteps | None = None) -> dict:
    return await build_agent_graph(steps or AgentSteps()).run(copy.deepcopy(_START), context=ctx)


def _roles(messages: list[dict]) -> list[str]:
    return [m["role"] for m in messages]


@pytest.mark.asyncio
async def test_a_reply_without_calls_is_the_answer():
    llm = ScriptedLLM(_answer("Hello! [Sources: none]"))
    ctx = _context(llm, sampling={"temperature": 0.1, "metadata": {"m": 1}})

    state = await _run(ctx)

    assert state["response"]["choices"][0]["message"]["content"] == "Hello! [Sources: none]"
    assert state.get("rounds") == 0 and not state.get("finalized")
    assert len(llm.calls) == 1
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == ["lookup"]
    assert llm.calls[0]["temperature"] == 0.1 and llm.calls[0]["metadata"] == {"m": 1}


@pytest.mark.asyncio
async def test_the_model_reads_its_calls_results_then_answers():
    toys = Lookup()
    llm = ScriptedLLM(_calls("acme", "orange"), _answer("ACME and Orange. [Sources: 1, 2]"))
    ctx = _context(llm, toys)

    state = await _run(ctx)

    assert toys.words == ["acme", "orange"]
    assert state["rounds"] == 1
    sent = llm.calls[1]["messages"]
    assert _roles(sent) == ["system", "user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in sent[3:]] == [c["id"] for c in sent[2]["tool_calls"]]
    assert [m["content"] for m in sent[3:]] == ["[Source 1] about acme", "[Source 2] about orange"]
    assert [chunk.id for chunk in ctx.tool_context.sources.entries] == ["acme", "orange"]
    assert "tools" in llm.calls[1], "the model is offered the tools again after reading the results"


@pytest.mark.asyncio
async def test_when_the_rounds_run_out_the_model_answers_without_tools():
    llm = Tireless()

    state = await _run(_context(llm, max_tool_rounds=2))

    assert state["rounds"] == 2 and state["finalized"] is True
    assert len(llm.calls) == 3
    last = llm.calls[-1]
    assert "tools" not in last
    assert last["messages"][0]["content"].endswith(FINALIZE_INSTRUCTION)
    assert _roles(last["messages"])[-1] == "tool", "it answers from the results it read"
    assert state["response"]["choices"][0]["message"]["content"] == "What I found. [Sources: 1]"


@pytest.mark.asyncio
async def test_a_call_already_made_is_answered_without_running_again():
    toys = Lookup()
    llm = ScriptedLLM(_calls("acme"), _calls("acme"), _answer("ACME. [Sources: 1]"))

    state = await _run(_context(llm, toys))

    assert toys.words == ["acme"]
    second_result = state["messages"][-1]
    assert second_result["role"] == "tool"
    assert second_result["content"] == "Error: you already made this call: its result is above."


@pytest.mark.asyncio
async def test_a_round_runs_at_most_its_cap_of_calls():
    toys = Lookup()
    words = [f"w{i}" for i in range(MAX_CALLS_PER_ROUND + 2)]
    llm = ScriptedLLM(_calls(*words), _answer("done"))

    state = await _run(_context(llm, toys))

    assert toys.words == words[:MAX_CALLS_PER_ROUND]
    results = [m for m in state["messages"] if m["role"] == "tool"]
    assert len(results) == len(words), "every call gets an answer"
    assert all("Make this one again if you still need it." in m["content"] for m in results[MAX_CALLS_PER_ROUND:])


@pytest.mark.asyncio
async def test_a_failing_tool_is_an_error_the_model_reads():
    class Broken:
        @tool("lookup", "Look a word up.")
        async def lookup(self, ctx: ToolContext, word: str) -> str:
            raise RuntimeError("database password is hunter2")

    llm = ScriptedLLM(_calls("acme"), _answer("I could not look it up."))

    state = await _run(_context(llm, Broken()))

    assert state["messages"][-1]["content"] == "Error: 'lookup' failed."
    assert state["response"]["choices"][0]["message"]["content"] == "I could not look it up."


@pytest.mark.asyncio
async def test_when_the_window_is_full_the_model_answers_with_old_results_left_out():
    llm = ScriptedLLM(_calls("acme"), _answer("From what fits. [Sources: none]"))
    ctx = _context(llm, Lookup(text="word " * 400), max_prompt_tokens=150)

    state = await _run(ctx)

    assert state["finalized"] is True and state["rounds"] == 1
    assert len(llm.calls) == 2
    last = llm.calls[-1]
    assert "tools" not in last
    assert last["messages"][-1] == {"role": "tool", "tool_call_id": "call00000", "content": LEFT_OUT_RESULT}


@pytest.mark.asyncio
async def test_a_conversation_too_long_for_the_tools_is_answered_without_them():
    llm = ScriptedLLM(_answer("Short answer."))

    state = await _run(_context(llm, max_prompt_tokens=5))

    assert state["finalized"] is True
    assert "tools" not in llm.calls[0]


@pytest.mark.asyncio
async def test_the_recursion_limit_ends_with_an_answer_not_an_error():
    class Unbounded(AgentSteps):
        """Steps whose round limit never stops the loop: only LangGraph's recursion limit can."""

        def may_call_tools(self, state, ctx):
            return True

    llm = Tireless()

    state = await _run(_context(llm, max_tool_rounds=2), Unbounded())

    assert state["finalized"] is True
    assert state["response"]["choices"][0]["message"]["content"] == "What I found. [Sources: 1]"
    assert "tools" not in llm.calls[-1]


# --------------------------------------------------------------------------- #
# Streaming: the answer's text goes out as the model writes it
# --------------------------------------------------------------------------- #


def _chunk(delta: dict, finish_reason=None) -> str:
    return "data: " + json.dumps(
        {"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    )


def _streamed(reply: dict) -> list[str]:
    """*reply* as vLLM streams it: the role, each call in two pieces, the text word by word, the finish."""
    message = reply["choices"][0]["message"]
    lines = [_chunk({"role": "assistant"})]
    calls = message.get("tool_calls") or []
    for index, call in enumerate(calls):
        name, arguments = call["function"]["name"], call["function"]["arguments"]
        first = {
            "index": index,
            "id": call["id"],
            "type": "function",
            "function": {"name": name, "arguments": arguments[:3]},
        }
        lines.append(_chunk({"tool_calls": [first]}))
        lines.append(_chunk({"tool_calls": [{"index": index, "function": {"arguments": arguments[3:]}}]}))
    words = (message.get("content") or "").split(" ")
    lines.extend(_chunk({"content": word if i == 0 else f" {word}"}) for i, word in enumerate(words) if word)
    lines += [_chunk({}, "tool_calls" if calls else "stop"), "data: [DONE]"]
    return lines


class StreamingLLM(ScriptedLLM):
    """``ScriptedLLM`` whose replies are streamed."""

    async def stream_chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), "stream": True, **kwargs})
        reply = self._replies.pop(0) if self._replies else _answer("done [Sources: none]")
        for line in _streamed(reply() if callable(reply) else reply):
            yield line


async def _stream(ctx: AgentContext, steps: AgentSteps | None = None) -> tuple[list[str], dict]:
    lines, state = [], None
    async for mode, value in build_agent_graph(steps or AgentSteps()).stream(copy.deepcopy(_START), context=ctx):
        if mode == "custom":
            lines.append(value)
        else:
            state = value
    return lines, state


def _texts(lines: list[str]) -> list[str]:
    return [json.loads(line[6:])["choices"][0]["delta"].get("content") for line in lines]


@pytest.mark.asyncio
async def test_streamed_the_answer_goes_out_word_by_word_and_the_tool_round_not_at_all():
    llm = StreamingLLM(_calls("acme"), _answer("ACME is a client. [Sources: 1]"))
    ctx = replace(_context(llm), stream=True)

    lines, state = await _stream(ctx)

    assert _texts(lines) == ["ACME", " is", " a", " client.", " [Sources:", " 1]", None]
    assert json.loads(lines[-1][6:])["choices"][0]["finish_reason"] == "stop"
    assert not any("tool_calls" in line for line in lines)
    assert all(call.get("stream") for call in llm.calls), "every reply is streamed"
    assert state["rounds"] == 1
    assert state["messages"][2]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"word": "acme"}'}
    assert [chunk.id for chunk in ctx.tool_context.sources.entries] == ["acme"]


@pytest.mark.asyncio
async def test_streamed_finalize_writes_its_answer_too():
    llm = StreamingLLM(_calls("acme"), _answer("From what I found. [Sources: 1]"))
    ctx = replace(_context(llm, max_tool_rounds=1), stream=True)

    lines, state = await _stream(ctx)

    assert state["finalized"] is True
    assert "".join(t or "" for t in _texts(lines)) == "From what I found. [Sources: 1]"
    assert "tools" not in llm.calls[-1]


@pytest.mark.asyncio
async def test_streamed_the_recursion_limit_still_ends_with_an_answer():
    class Unbounded(AgentSteps):
        def may_call_tools(self, state, ctx):
            return True

    class TirelessStream(StreamingLLM):
        async def stream_chat(self, messages, **kwargs):
            self._replies = [_calls(f"w{len(self.calls)}") if "tools" in kwargs else _answer("Best effort.")]
            async for line in super().stream_chat(messages, **kwargs):
                yield line

    lines, state = await _stream(replace(_context(TirelessStream(), max_tool_rounds=1), stream=True), Unbounded())

    assert state["finalized"] is True
    assert "".join(t or "" for t in _texts(lines)) == "Best effort."


@pytest.mark.parametrize("max_tool_rounds", [3, 1], ids=["answers", "finalizes"])
@pytest.mark.asyncio
async def test_streamed_or_not_the_agent_takes_the_same_trajectory(max_tool_rounds):
    """One graph: streaming changes how each reply reaches us, never which steps run or what they do."""
    replies = [_calls("acme", "orange"), _calls("acme", "zeta"), _answer("ACME. [Sources: 1]")]
    plain_ctx = _context(ScriptedLLM(*copy.deepcopy(replies)), max_tool_rounds=max_tool_rounds)
    streamed_ctx = replace(
        _context(StreamingLLM(*copy.deepcopy(replies)), max_tool_rounds=max_tool_rounds), stream=True
    )

    plain = await _run(plain_ctx)
    _lines, streamed = await _stream(streamed_ctx)

    for key in ("messages", "rounds", "calls_made", "finalized"):
        assert streamed.get(key) == plain.get(key), key
    assert [c.id for c in streamed_ctx.tool_context.sources.entries] == [
        c.id for c in plain_ctx.tool_context.sources.entries
    ]
    assert [{k: v for k, v in call.items() if k != "stream"} for call in streamed_ctx.llm.calls] == plain_ctx.llm.calls
