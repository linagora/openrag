"""Unit tests for the ``agentic`` graph (``services.chat.graphs.agentic``) and its steps (``services.chat.agentic``).

The compiled graph runs with the real ``AgentSteps``, a scripted model and a
toy tool, so each test reads as one turn of the agent: what the model was
offered, which calls ran, how the turn ended.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from core.chat.agent import FINALIZE_INSTRUCTION, LEFT_OUT_RESULT, MAX_CALLS_PER_ROUND
from core.chat.agent_profile import AgentProfile
from core.chat.agent_trace import AgentTrace
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
    assert last["messages"][-1] == {"role": "user", "content": FINALIZE_INSTRUCTION}, "read last, after the results"
    assert _roles(last["messages"])[-2] == "tool", "it answers from the results it read"
    assert last["messages"][0] == llm.calls[0]["messages"][0], "the system prompt is unchanged"
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
async def test_the_calls_of_a_round_run_at_once_and_their_results_keep_the_call_order():
    class Slow:
        def __init__(self) -> None:
            self.all_started = asyncio.Barrier(3)
            self.finished: list[str] = []

        @tool("lookup", "Look a word up.")
        async def lookup(self, ctx: ToolContext, word: str) -> str:
            # Each call waits for the three to have started: run one after another, the first never would.
            await asyncio.wait_for(self.all_started.wait(), 1)
            await asyncio.sleep({"a": 0.03, "b": 0.02, "c": 0.01}[word])
            self.finished.append(word)
            return f"about {word}"

    toys = Slow()
    llm = ScriptedLLM(_calls("a", "b", "c"), _answer("done"))

    state = await _run(_context(llm, toys))

    assert toys.finished == ["c", "b", "a"], "the last call finished first: they ran at once"
    results = [m for m in state["messages"] if m["role"] == "tool"]
    assert [m["content"] for m in results] == ["about a", "about b", "about c"]
    assert [m["tool_call_id"] for m in results] == ["call00000", "call00001", "call00002"]


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
    assert last["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call00000", "content": LEFT_OUT_RESULT},
        {"role": "user", "content": FINALIZE_INSTRUCTION},
    ]


@pytest.mark.asyncio
async def test_a_conversation_too_long_for_the_tools_is_answered_without_them():
    llm = ScriptedLLM(_answer("Short answer."))

    state = await _run(_context(llm, max_prompt_tokens=5))

    assert state["finalized"] is True
    assert "tools" not in llm.calls[0]


class Unbounded(AgentSteps):
    """Steps whose round limit never stops the loop: only LangGraph's recursion limit can."""

    def may_call_tools(self, state, ctx):
        return True


@pytest.mark.asyncio
async def test_the_recursion_limit_ends_with_an_answer_not_an_error():
    llm = Tireless()

    state = await _run(_context(llm, max_tool_rounds=2), Unbounded())

    assert state["finalized"] is True
    assert state["response"]["choices"][0]["message"]["content"] == "What I found. [Sources: 1]"
    assert "tools" not in llm.calls[-1]


@pytest.mark.asyncio
async def test_each_step_is_logged_as_it_starts_with_the_calls_a_tools_round_runs(logged_steps):
    llm = ScriptedLLM(_calls("acme", "orange"), _answer("ACME and Orange. [Sources: 1, 2]"))

    await _run(_context(llm))

    assert logged_steps == [
        ("agentic:agent", {"round": 1}),
        ("agentic:tools", {"round": 1, "calls": 2, "tools": 'lookup {"word": "acme"}, lookup {"word": "orange"}'}),
        ("agentic:agent", {"round": 2}),
    ]


@pytest.mark.parametrize(
    ("context", "steps", "reason"),
    [
        ({"max_tool_rounds": 1}, None, "no rounds left"),
        ({"max_prompt_tokens": 5}, None, "window full"),
        ({"max_tool_rounds": 1}, Unbounded(), "recursion limit"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_is_logged_with_why_the_model_answers_without_tools(logged_steps, context, steps, reason):
    await _run(_context(Tireless(), **context), steps)

    assert logged_steps[-1] == ("agentic:finalize", {"reason": reason})


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


def _steps(lines: list[str]) -> list[dict]:
    return [chunk["extra"]["agent_step"] for chunk in map(_parsed, lines) if "agent_step" in chunk.get("extra", {})]


def _parsed(line: str) -> dict:
    return json.loads(line[6:])


def _texts(lines: list[str]) -> list[str]:
    """The answer's text of each line that is not an agent step."""
    return [
        chunk["choices"][0]["delta"].get("content")
        for chunk in map(_parsed, lines)
        if "agent_step" not in chunk.get("extra", {})
    ]


@pytest.mark.asyncio
async def test_streamed_the_answer_goes_out_word_by_word_after_each_call_is_announced():
    llm = StreamingLLM(_calls("acme"), _answer("ACME is a client. [Sources: 1]"))
    ctx = replace(_context(llm), stream=True)

    lines, state = await _stream(ctx)

    assert _steps(lines) == [
        {"type": "tool_call", "round": 1, "id": "call00000", "tool": "lookup", "arguments": {"word": "acme"}},
        {
            "type": "tool_result",
            "round": 1,
            "id": "call00000",
            "tool": "lookup",
            "status": "ok",
            "summary": "[Source 1] about acme",
        },
    ]
    assert _texts(lines) == ["ACME", " is", " a", " client.", " [Sources:", " 1]", None]
    assert json.loads(lines[-1][6:])["choices"][0]["finish_reason"] == "stop"
    assert not any("tool_calls" in line for line in lines), "the model's calls are the agent's, not the client's"
    assert all(call.get("stream") for call in llm.calls), "every reply is streamed"
    assert state["rounds"] == 1
    assert state["messages"][2]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"word": "acme"}'}
    assert [chunk.id for chunk in ctx.tool_context.sources.entries] == ["acme"]


class Thinker(ScriptedLLM):
    """Thinks before each reply and says what it will do before calling a tool, as kimi-k3 does."""

    async def stream_chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), "stream": True, **kwargs})
        yield _chunk({"role": "assistant", "reasoning_content": "Let me see.", "content": ""})
        if len(self.calls) == 1:
            yield _chunk({"content": "I'll look ACME up."})
            call = {"index": 0, "id": "call00000", "type": "function", "function": {"name": "lookup", "arguments": ""}}
            yield _chunk({"tool_calls": [call]})
            yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"word": "acme"}'}}]})
            yield _chunk({}, "tool_calls")
        else:
            yield _chunk({"content": "ACME is a client. [Sources: 1]"})
            yield _chunk({}, "stop")
        yield "data: [DONE]"


def _seen(lines: list[str]) -> list[tuple[str, object]]:
    """What the client sees of each line: thinking, a step, the answer's text, the finish."""
    seen = []
    for chunk in map(_parsed, lines):
        if "agent_step" in chunk.get("extra", {}):
            seen.append(("step", chunk["extra"]["agent_step"]["type"]))
            continue
        choice = chunk["choices"][0]
        if choice["finish_reason"]:
            seen.append(("finish", choice["finish_reason"]))
        elif "reasoning_content" in choice["delta"]:
            seen.append(("thinking", choice["delta"]["reasoning_content"]))
        else:
            seen.append(("answer", choice["delta"]["content"]))
    return seen


@pytest.mark.asyncio
async def test_streamed_the_client_sees_the_agent_think_and_call_then_the_answer():
    llm = Thinker()

    lines, state = await _stream(replace(_context(llm), stream=True))

    assert _seen(lines) == [
        ("thinking", "Let me see."),
        ("thinking", "I'll look ACME up."),  # said before a call: not the answer
        ("step", "tool_call"),
        ("step", "tool_result"),
        ("thinking", "Let me see."),
        ("answer", "ACME is a client. [Sources: 1]"),
        ("finish", "stop"),
    ]
    assert state["messages"][2]["content"] == "I'll look ACME up.", "the model still reads what it said"
    assert state["response"]["choices"][0]["message"]["content"] == "ACME is a client. [Sources: 1]"


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


# --------------------------------------------------------------------------- #
# Tracing: what the agent did, for extra.agent_trace
# --------------------------------------------------------------------------- #


class TracedLookup:
    """``lookup(word)`` shown as a passage is, recording into the call's retrieval trace as a search does."""

    def __init__(self) -> None:
        self.traces: list = []

    @tool("lookup", "Look a word up.")
    async def lookup(self, ctx: ToolContext, word: str) -> str:
        self.traces.append(ctx.trace)
        if ctx.trace is not None:
            ctx.trace.record_stage("final", status="complete", candidates=[])
        number = ctx.sources.number(Chunk(id=word, text=word, metadata={}), key=word)
        return f"1 excerpt.\n\n[Source {number}]\nabout {word}"


def _traced(ctx: AgentContext) -> tuple[AgentContext, AgentTrace]:
    trace = AgentTrace(
        request_id="req",
        profile=ctx.profile.name,
        max_tool_rounds=ctx.profile.max_tool_rounds,
        max_prompt_tokens=ctx.max_prompt_tokens,
    )
    return replace(ctx, trace=trace), trace


def _finished(trace: AgentTrace) -> dict:
    return trace.finish(configuration_fingerprint="fp", total_seconds=1.0)


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "streamed"])
@pytest.mark.asyncio
async def test_traced_each_step_is_recorded_and_each_call_searches_into_a_trace_of_its_own(stream):
    lookup = TracedLookup()
    replies = (_calls("acme", "orange"), _answer("ACME and Orange. [Sources: 1, 2]"))
    llm = StreamingLLM(*replies) if stream else ScriptedLLM(*replies)
    ctx, trace = _traced(replace(_context(llm, lookup), stream=stream))

    await (_stream(ctx) if stream else _run(ctx))

    out = _finished(trace)
    assert [(step["step"], step["round"]) for step in out["steps"]] == [("agent", 1), ("tools", 1), ("agent", 2)]
    assert (out["rounds"], out["finalized"]) == (1, False)
    first_reply = out["steps"][0]
    assert first_reply["tool_calls"] == 2
    assert out["steps"][2]["finish_reason"] == "stop"
    assert first_reply["prompt_tokens"] > 0
    calls = out["steps"][1]["calls"]
    assert [(call["arguments"], call["status"], call["sources"]) for call in calls] == [
        ({"word": "acme"}, "ok", [1]),
        ({"word": "orange"}, "ok", [2]),
    ]
    assert [call["retrieval_trace"]["request_id"] for call in calls] == ["req:round:1:call:1", "req:round:1:call:2"]
    assert lookup.traces[0] is not lookup.traces[1]
    assert ctx.tool_context.trace is None, "the request's tool context is left as it was"
    assert [chunk.id for chunk in ctx.tool_context.sources.entries] == ["acme", "orange"], "one source list"


@pytest.mark.asyncio
async def test_untraced_a_tool_gets_no_trace():
    lookup = TracedLookup()

    await _run(_context(ScriptedLLM(_calls("acme"), _answer("ACME. [Sources: 1]")), lookup))

    assert lookup.traces == [None]


@pytest.mark.asyncio
async def test_traced_a_call_answered_without_running_is_refused():
    lookup = TracedLookup()
    ctx, trace = _traced(_context(ScriptedLLM(_calls("acme"), _calls("acme"), _answer("ACME. [Sources: 1]")), lookup))

    await _run(ctx)

    second_round = _finished(trace)["steps"][3]
    assert second_round["step"] == "tools"
    assert [(call["status"], call["duration_seconds"], call["retrieval_trace"]) for call in second_round["calls"]] == [
        ("refused", None, None)
    ]
    assert len(lookup.traces) == 1


@pytest.mark.parametrize(
    ("context", "steps", "reason"),
    [
        ({"max_tool_rounds": 1}, None, "no rounds left"),
        ({"max_prompt_tokens": 5}, None, "window full"),
        ({"max_tool_rounds": 1}, Unbounded(), "recursion limit"),
    ],
)
@pytest.mark.asyncio
async def test_traced_finalize_is_recorded_with_why_the_model_answers_without_tools(context, steps, reason):
    ctx, trace = _traced(_context(Tireless(), **context))

    await _run(ctx, steps)

    out = _finished(trace)
    assert out["finalized"] is True
    assert (out["steps"][-1]["step"], out["steps"][-1]["reason"]) == ("finalize", reason)
    assert [step["reason"] for step in out["steps"][:-1] if step["step"] != "tools"] == [None] * (
        len([s for s in out["steps"][:-1] if s["step"] != "tools"])
    )
