"""Unit tests for ``core.chat.agent`` and ``core.chat.agent_profile``: the agent loop's rules and its profiles."""

from __future__ import annotations

import json

import pytest
from core.chat.agent import (
    LEFT_OUT_RESULT,
    assistant_turn,
    call_signature,
    leave_out_old_results,
    tool_calls,
    with_instruction,
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
    message = {"role": "assistant", "content": None, "tool_calls": [_call(), _call("read_file", '{"source": 1}', "x2")]}

    assert tool_calls(message) == [
        ToolCall("abc123def", "search_knowledge", '{"queries": ["x"]}'),
        ToolCall("x2", "read_file", '{"source": 1}'),
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


def test_the_instruction_joins_the_system_prompt_or_becomes_one():
    messages = [{"role": "system", "content": "Rules.\n"}, {"role": "user", "content": "q"}]

    assert with_instruction(messages, "Answer now.")[0] == {"role": "system", "content": "Rules.\n\nAnswer now."}
    assert messages[0]["content"] == "Rules.\n", "the caller's messages are not changed"
    assert with_instruction([{"role": "user", "content": "q"}], "Answer now.")[0] == {
        "role": "system",
        "content": "Answer now.",
    }


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
