"""The agent loop's rules with no I/O: reading the model's tool calls, the guards, fitting the conversation.

``services.chat.agentic`` runs the loop: the model calls tools, reads their
results, and answers. The rules here keep it bounded. A round runs at most
``MAX_CALLS_PER_ROUND`` calls; a call made earlier in the turn is not run
again (the loop guard), its result being already in the conversation; and
when the rounds run out or the conversation no longer fits the window, the
model answers once more without tools, told so by ``FINALIZE_INSTRUCTION``,
the oldest tool results left out until it fits.

A streamed reply comes as chat-completion chunks: ``StreamedReply`` puts it
back together as the reply a plain call returns, and ``text_line`` /
``finish_line`` are the chunks the client gets of it.
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


class StreamedReply:
    """A reply streamed as chat-completion chunks, put back together as a plain call returns it.

    Its text deltas are joined. A tool call comes in pieces keyed by its
    ``index``: the first carries its id and name, the next ones pieces of its
    arguments, which are joined (vLLM's Mistral parser streams them so). A
    call sent whole in one delta, or with its name repeated, reads the same.
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, dict] = {}
        self._base: dict = {}
        self.finish_reason: str | None = None

    def add(self, chunk: dict) -> str:
        """Take one chunk; return the text it adds, ``""`` for none."""
        choices = chunk.get("choices")
        if not choices:
            return ""
        self._base = {key: value for key, value in chunk.items() if key not in ("choices", "usage")}
        choice = choices[0]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        for position, piece in enumerate(delta.get("tool_calls") or []):
            if not isinstance(piece, dict):
                continue
            call = self._calls.setdefault(piece.get("index", position), {"id": None, "name": "", "arguments": ""})
            function = piece.get("function") or {}
            call["id"] = call["id"] or piece.get("id")
            call["name"] = call["name"] or function.get("name") or ""
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, str):
                call["arguments"] += arguments
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._text.append(text)
            return text
        return ""

    def response(self) -> dict:
        """The reply as a chat completion, as ``chat`` returns it."""
        message: dict = {"role": "assistant", "content": "".join(self._text) or None}
        if self._calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for _, call in sorted(self._calls.items())
            ]
        return {**self._base, "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}]}


def sse_chunk(line: str) -> dict | None:
    """The chunk an SSE ``data:`` line carries; ``None`` for any other line, ``[DONE]`` included."""
    if not line.startswith("data:"):
        return None
    body = line[len("data:") :].strip()
    if not body or body == "[DONE]":
        return None
    try:
        chunk = json.loads(body)
    except ValueError:
        return None
    return chunk if isinstance(chunk, dict) else None


def text_line(chunk: dict, text: str) -> str:
    """An SSE line giving the client *text* of *chunk*, without its tool calls or finish reason."""
    base = {key: value for key, value in chunk.items() if key not in ("choices", "usage")}
    return "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def finish_line(response: dict) -> str:
    """The SSE line ending the answer *response*: its finish reason, ``stop`` if it called tools or gave none."""
    choice = (response.get("choices") or [{}])[0]
    reason = choice.get("finish_reason")
    if reason in (None, "tool_calls", "function_call"):
        reason = "stop"
    base = {key: value for key, value in response.items() if key not in ("choices", "usage")}
    chunk = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    return "data: " + json.dumps(chunk)


__all__ = [
    "FINALIZE_INSTRUCTION",
    "LEFT_OUT_RESULT",
    "MAX_CALLS_PER_ROUND",
    "StreamedReply",
    "assistant_turn",
    "call_signature",
    "finish_line",
    "leave_out_old_results",
    "sse_chunk",
    "text_line",
    "tool_calls",
    "with_instruction",
]
