"""Unit tests for ``core.chat.agent`` and ``core.chat.agent_profile``: the agent loop's rules and its profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.chat.agent import (
    HELD_TEXT_CHARS,
    LEFT_OUT_RESULT,
    ReplyLines,
    StreamedReply,
    assistant_turn,
    call_signature,
    describe_call,
    finish_line,
    leave_out_old_results,
    sse_chunk,
    step_line,
    text_line,
    tool_calls,
)
from core.chat.agent_profile import AgentProfiles
from core.config.agentic import DEFAULT_AGENT_TOOLS, AgenticConfig, AgentProfileConfig
from core.tools import ToolCall
from core.utils.exceptions import ValidationError


def _call(name="search_knowledge", arguments='{"queries": ["x"]}', call_id="abc123def"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# --------------------------------------------------------------------------- #
# Reading the model's tool calls
# --------------------------------------------------------------------------- #


def test_tool_calls_are_read_with_their_id_name_and_arguments():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [_call(), _call("read_passage", '{"source": 1}', "x2")],
    }

    assert tool_calls(message) == [
        ToolCall("abc123def", "search_knowledge", '{"queries": ["x"]}'),
        ToolCall("x2", "read_passage", '{"source": 1}'),
    ]


def test_arguments_sent_as_an_object_are_serialized_and_missing_ones_are_empty():
    message = {"tool_calls": [_call(arguments={"queries": ["é"]}), _call(arguments=None)]}

    assert [call.arguments for call in tool_calls(message)] == ['{"queries": ["é"]}', "{}"]


def test_a_call_without_an_id_gets_one_and_one_without_a_name_is_dropped():
    message = {"tool_calls": [_call(call_id=""), {"id": "y", "function": {"arguments": "{}"}}, "junk"]}

    calls = tool_calls(message)

    assert len(calls) == 1
    assert len(calls[0].id) == 9 and calls[0].id.isalnum()


def test_no_tool_calls_is_an_answer():
    assert tool_calls({"role": "assistant", "content": "The answer."}) == []


def test_the_assistant_turn_keeps_its_text_and_the_calls_that_get_answered():
    calls = [ToolCall("id1", "find_files", '{"name": "acme"}')]

    assert assistant_turn({"content": "", "tool_calls": ["raw"]}, calls) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "id1", "type": "function", "function": {"name": "find_files", "arguments": '{"name": "acme"}'}}
        ],
    }
    assert assistant_turn({"content": "Let me look."}, calls)["content"] == "Let me look."


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #


def test_two_calls_are_the_same_whatever_the_order_or_spacing_of_their_arguments():
    first = ToolCall("a", "search_file", json.dumps({"partition": "p", "file_id": "f"}))
    second = ToolCall("b", "search_file", '{ "file_id":"f",  "partition":"p" }')
    other = ToolCall("c", "search_file", json.dumps({"partition": "p", "file_id": "g"}))

    assert call_signature(first) == call_signature(second)
    assert call_signature(first) != call_signature(other)
    assert call_signature(ToolCall("d", "x", "not json")) == "x:not json"


def test_a_call_is_described_with_its_arguments_compact_and_capped():
    assert describe_call(ToolCall("a", "search_knowledge", '{ "queries" : ["préavis"] }')) == (
        'search_knowledge {"queries": ["préavis"]}'
    )
    assert describe_call(ToolCall("b", "read_passage", "not json")) == "read_passage not json"
    long = describe_call(ToolCall("c", "search_knowledge", json.dumps({"queries": ["x" * 400]})))
    assert len(long) == len("search_knowledge ") + 300 and long.endswith("…")


def test_the_oldest_tool_results_are_left_out_until_the_conversation_fits():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "1", "content": "old " * 50},
        {"role": "tool", "tool_call_id": "2", "content": "new " * 50},
    ]

    def fits(candidate):
        return sum(len(m["content"]) for m in candidate) < 300

    out = leave_out_old_results(messages, fits)

    assert [m["content"] for m in out] == ["s", LEFT_OUT_RESULT, "new " * 50]
    assert messages[1]["content"] == "old " * 50
    assert leave_out_old_results(messages, lambda _m: True) == messages
    everything = leave_out_old_results(messages, lambda _m: False)
    assert [m["content"] for m in everything[1:]] == [LEFT_OUT_RESULT, LEFT_OUT_RESULT]


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def test_the_default_profile_offers_every_read_only_tool():
    profile = AgentProfiles(AgenticConfig()).get(None)

    assert profile.name == "default"
    assert profile.tools == DEFAULT_AGENT_TOOLS
    assert profile.prompt_name is None
    assert profile.temperature == 0.2
    assert profile.max_tool_rounds == 6


def test_a_profile_is_picked_by_name_and_an_unknown_name_is_a_400():
    config = AgenticConfig(
        profiles={
            "default": AgentProfileConfig(),
            "legal": AgentProfileConfig(prompt_name="legal", tools=["find_files"]),
        }
    )
    profiles = AgentProfiles(config)

    assert profiles.get("legal").tools == ("find_files",)
    assert profiles.get("legal").prompt_name == "legal"
    with pytest.raises(ValidationError) as error:
        profiles.get("lawyer")
    assert error.value.status_code == 400
    assert "Unknown agent_profile 'lawyer'. Profiles: default, legal." in error.value.message


def test_the_default_profile_must_exist():
    with pytest.raises(ValueError, match="agentic.default_profile 'legal' is not a profile"):
        AgenticConfig(default_profile="legal")


# --------------------------------------------------------------------------- #
# A streamed reply, put back together
# --------------------------------------------------------------------------- #

_RECORDED = Path(__file__).parents[2] / "services/chat/fixtures/mistral_small_tool_call.sse"


def _reassembled(lines) -> StreamedReply:
    reply = StreamedReply()
    for line in lines:
        chunk = sse_chunk(line)
        if chunk is not None:
            reply.add(chunk)
    return reply


def test_a_tool_call_streamed_by_vllm_for_mistral_is_put_back_together():
    response = _reassembled(_RECORDED.read_text().splitlines()).response()

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "vu6TgUHxP",
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "arguments": '{"queries": ["notice period in the employment contract", "number of holidays for employees"]}',
            },
        }
    ]
    assert tool_calls(choice["message"]) == [
        ToolCall(
            "vu6TgUHxP",
            "search_knowledge",
            '{"queries": ["notice period in the employment contract", "number of holidays for employees"]}',
        )
    ]
    assert response["id"] == "chatcmpl-bd1e0fc1dc53ffa8"


def _line(delta: dict, finish_reason=None, **extra) -> str:
    return "data: " + json.dumps(
        {"id": "x", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra}
    )


def test_calls_sent_whole_or_with_their_name_repeated_read_the_same():
    whole = {"name": "find_files", "arguments": {"name": "acme"}}
    lines = [
        _line({"tool_calls": [{"id": "a", "function": whole}]}),  # whole, no index (Ollama)
        _line({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "read_passage", "arguments": '{"sou'}}]}),
        _line({"tool_calls": [{"index": 1, "function": {"name": "read_passage", "arguments": 'rce": 2}'}}]}),
        _line({}, "tool_calls"),
    ]

    calls = _reassembled(lines).response()["choices"][0]["message"]["tool_calls"]

    assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in calls] == [
        ("a", "find_files", '{"name": "acme"}'),
        ("b", "read_passage", '{"source": 2}'),
    ]


def test_streamed_text_is_joined_and_each_chunk_gives_its_own():
    reply = StreamedReply()
    texts = [
        reply.add(sse_chunk(line))
        for line in (_line({"role": "assistant"}), _line({"content": "Three "}), _line({"content": "months."}, "stop"))
    ]

    assert texts == ["", "Three ", "months."]
    assert reply.response()["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "Three months."},
        "finish_reason": "stop",
    }
    assert reply.add({"choices": [], "usage": {"total_tokens": 3}}) == "", "a usage chunk adds nothing"


@pytest.mark.parametrize("line", ["", ": comment", "data: [DONE]", "data: not json", "data: []", "event: ping"])
def test_lines_without_a_chunk_are_skipped(line):
    assert sse_chunk(line) is None


def test_the_client_gets_the_text_without_the_calls_and_a_finish_of_its_own():
    chunk = {
        "id": "x",
        "model": "m",
        "usage": {"t": 1},
        "choices": [{"index": 0, "delta": {"content": "Hi", "tool_calls": [{}]}, "finish_reason": "tool_calls"}],
    }

    assert json.loads(text_line(chunk, "Hi")[len("data: ") :]) == {
        "id": "x",
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}],
    }
    finish = json.loads(finish_line({"id": "x", "choices": [{"finish_reason": "tool_calls"}]})[len("data: ") :])
    assert finish["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert finish["object"] == "chat.completion.chunk"
    kept = json.loads(finish_line({"choices": [{"finish_reason": "length"}]})[len("data: ") :])
    assert kept["choices"][0]["finish_reason"] == "length"


# --------------------------------------------------------------------------- #
# What the client sees of a streamed reply
# --------------------------------------------------------------------------- #

_KIMI = Path(__file__).parents[2] / "services/chat/fixtures/kimi_k3_thinking_tool_call.sse"


def _seen(lines: list[str]) -> list[tuple[str, str]]:
    """Each line as (``thinking`` | ``answer``, its text)."""
    out = []
    for line in lines:
        delta = json.loads(line[len("data: ") :])["choices"][0]["delta"]
        out.append(
            ("thinking", delta["reasoning_content"]) if "reasoning_content" in delta else ("answer", delta["content"])
        )
    return out


def _play(chunks: list[dict]) -> tuple[ReplyLines, list[str]]:
    reply = ReplyLines()
    lines = [line for chunk in chunks for line in reply.add(chunk)]
    return reply, lines + reply.end()


def _delta(finish_reason=None, **delta) -> dict:
    return {"id": "x", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def test_kimi_s_thinking_and_its_sentence_before_the_call_are_shown_as_thinking():
    chunks = [c for c in map(sse_chunk, _KIMI.read_text().splitlines()) if c is not None]

    reply, lines = _play(chunks)

    seen = _seen(lines)
    assert {kind for kind, _ in seen} == {"thinking"}, "nothing of a reply that calls a tool is the answer"
    thinking = "".join(text for _, text in seen)
    assert thinking.startswith("The user is asking about")
    assert thinking.endswith("I'll search your documents for information on digital sovereignty.")
    call = reply.reply.response()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "search_knowledge"
    assert json.loads(call["arguments"])["queries"][0] == "digital sovereignty"


def test_a_short_answer_is_held_to_the_end_then_given_as_the_answer():
    reply, lines = _play([_delta(reasoning="Easy."), _delta(content="Three "), _delta(content="months.")])

    assert _seen(lines) == [("thinking", "Easy."), ("answer", "Three "), ("answer", "months.")]
    assert reply.reply.response()["choices"][0]["message"]["content"] == "Three months."


def test_a_long_answer_streams_once_past_the_held_text():
    words = [_delta(content="word ") for _ in range(HELD_TEXT_CHARS // 5 + 3)]
    reply = ReplyLines()

    per_chunk = [reply.add(chunk) for chunk in words]

    released = next(i for i, lines in enumerate(per_chunk) if lines)
    assert released == HELD_TEXT_CHARS // 5 - 1 and len(per_chunk[released]) == released + 1
    assert all(len(lines) == 1 for lines in per_chunk[released + 1 :]), "then each piece goes out as it comes"
    assert reply.end() == []


def test_step_lines_carry_their_event_in_extra():
    chunk = json.loads(step_line({"type": "tool_call", "tool": "find_files", "arguments": {"name": "été"}})[6:])

    assert chunk["extra"] == {"agent_step": {"type": "tool_call", "tool": "find_files", "arguments": {"name": "été"}}}
    assert chunk["choices"] == [{"index": 0, "delta": {}, "finish_reason": None}]
