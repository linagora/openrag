"""The agent loop's rules with no I/O: reading the model's tool calls, the guards, fitting the conversation.

``services.chat.agentic`` runs the loop: the model calls tools, reads their
results, and answers. The rules here keep it bounded. A round runs at most
``MAX_CALLS_PER_ROUND`` calls; a call made earlier in the turn is not run
again (the loop guard), its result being already in the conversation; and
when the rounds run out or the conversation no longer fits the window, the
model answers once more without tools, told so by ``FINALIZE_INSTRUCTION``,
the oldest tool results left out until it fits.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from core.tools import ToolCall

#: Calls a round runs; the model is told to make the others again if it still needs them.
MAX_CALLS_PER_ROUND = 5

FINALIZE_INSTRUCTION = (
    "You can't call tools any more. Answer the user's last message now, from the passages your tools "
    "returned above and following your rules. Say what you could not find."
)

#: What a tool result left out of the conversation reads, once it no longer fits.
LEFT_OUT_RESULT = "(Left out: the conversation grew too long to keep this result.)"


def tool_calls(message: dict) -> list[ToolCall]:
    """The tool calls of an assistant *message*, each with an id and its arguments as a JSON string.

    A call without a tool name is dropped: nothing could answer it. Arguments
    a provider sends as an object are serialized; a missing id is made up, as
    every call must be answered under its id.
    """
    calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") if isinstance(raw, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict | list):
            arguments = json.dumps(arguments, ensure_ascii=False)
        elif not isinstance(arguments, str):
            arguments = "{}"
        call_id = raw.get("id")
        if not isinstance(call_id, str) or not call_id:
            # Nine letters and digits: the id shape Mistral's chat template accepts.
            call_id = "call" + uuid.uuid4().hex[:5]
        calls.append(ToolCall(call_id, name, arguments))
    return calls


def assistant_turn(message: dict, calls: list[ToolCall]) -> dict:
    """The assistant's turn as the conversation keeps it: its text, if any, and the *calls* that get answered."""
    content = message.get("content")
    return {
        "role": "assistant",
        "content": content if isinstance(content, str) and content else None,
        "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
            for call in calls
        ],
    }


def call_signature(call: ToolCall) -> str:
    """What makes two calls the same: the tool and its arguments, whatever their order or spacing."""
    try:
        arguments = json.dumps(json.loads(call.arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except ValueError:
        arguments = call.arguments
    return f"{call.name}:{arguments}"


def with_instruction(messages: list[dict], instruction: str) -> list[dict]:
    """*messages* with *instruction* added to the leading system prompt, or as one when there is none."""
    out = [dict(message) for message in messages]
    if out and out[0].get("role") == "system":
        existing = out[0].get("content") or ""
        out[0]["content"] = f"{existing.rstrip()}\n\n{instruction}"
    else:
        out.insert(0, {"role": "system", "content": instruction})
    return out


def leave_out_old_results(messages: list[dict], fits: Callable[[list[dict]], bool]) -> list[dict]:
    """*messages* with the oldest tool results replaced by ``LEFT_OUT_RESULT`` until *fits* accepts them.

    The calls stay, so every call still has its answer. When even leaving out
    every result isn't enough, that is what is returned.
    """
    out = [dict(message) for message in messages]
    if fits(out):
        return out
    for message in out:
        if message.get("role") == "tool" and message.get("content") != LEFT_OUT_RESULT:
            message["content"] = LEFT_OUT_RESULT
            if fits(out):
                break
    return out


__all__ = [
    "FINALIZE_INSTRUCTION",
    "LEFT_OUT_RESULT",
    "MAX_CALLS_PER_ROUND",
    "assistant_turn",
    "call_signature",
    "leave_out_old_results",
    "tool_calls",
    "with_instruction",
]
