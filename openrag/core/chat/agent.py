"""The agent loop's rules with no I/O: reading the model's tool calls, the guards, fitting the conversation.

``services.chat.agentic`` runs the loop: the model calls tools, reads their
results, and answers. The rules here keep it bounded. A round runs at most
``MAX_CALLS_PER_ROUND`` calls; a call made earlier in the turn is not run
again (the loop guard), its result being already in the conversation; and
when the rounds run out or the conversation no longer fits the window, the
model answers once more without tools, told so by ``FINALIZE_INSTRUCTION``
after the results, the oldest of them left out until it fits.

A streamed reply comes as chat-completion chunks: ``StreamedReply`` puts it
back together as the reply a plain call returns, and ``ReplyLines`` gives the
client what it should see of it as it comes: the model's thinking, then the
answer's text. A reply's text is the answer only if no tool call follows it;
the sentence a model writes before a call ("I'll search your documents") is
shown as thinking. ``step_line`` tells the client a tool call started or ended.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from core.tools import ToolCall
from core.utils.stream_progress import AGENT_STEP, reasoning_text

#: Calls a round runs; the model is told to make the others again if it still needs them.
MAX_CALLS_PER_ROUND = 5

#: What ``finalize`` adds after the tool results, as a user message: the last thing
#: the model reads. Added to the system prompt instead, thousands of tokens of
#: results earlier, a model (kimi-k3) went on with its plan ("let me read one more
#: passage") and that was the answer. A user message, since Mistral's template
#: refuses a system message after a tool result.
FINALIZE_INSTRUCTION = (
    "You can't call tools any more. Answer my previous message now, from the passages your tools "
    "returned and following your rules, without saying you will search or read more. "
    "Say what you could not find."
)

#: What a tool result left out of the conversation reads, once it no longer fits.
LEFT_OUT_RESULT = "(Left out: the conversation grew too long to keep this result.)"

#: Characters of a streamed reply's text held back until it is clear whether a tool call follows.
HELD_TEXT_CHARS = 300


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


#: Characters of a call's arguments a log line shows.
_LOGGED_ARGUMENTS_CHARS = 300


def describe_call(call: ToolCall) -> str:
    """*call* as a log line shows it: the tool and its arguments, compact, ``search_knowledge {"queries": [...]}``."""
    try:
        arguments = json.dumps(json.loads(call.arguments or "{}"), ensure_ascii=False)
    except ValueError:
        arguments = call.arguments
    if len(arguments) > _LOGGED_ARGUMENTS_CHARS:
        arguments = arguments[: _LOGGED_ARGUMENTS_CHARS - 1] + "…"
    return f"{call.name} {arguments}"


def call_signature(call: ToolCall) -> str:
    """What makes two calls the same: the tool and its arguments, whatever their order or spacing."""
    try:
        arguments = json.dumps(json.loads(call.arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except ValueError:
        arguments = call.arguments
    return f"{call.name}:{arguments}"


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

    @property
    def calls_tools(self) -> bool:
        """Whether the reply has started a tool call."""
        return bool(self._calls)

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
    return _delta_line(chunk, {"content": text})


def reasoning_line(chunk: dict, text: str) -> str:
    """An SSE line giving the client *text* of *chunk* as the model's thinking."""
    return _delta_line(chunk, {"reasoning_content": text})


def _delta_line(chunk: dict, delta: dict) -> str:
    base = {key: value for key, value in chunk.items() if key not in ("choices", "usage", "extra")}
    return "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


def step_line(event: dict) -> str:
    """An SSE line telling the client of an agent step, *event*, in its ``extra``."""
    chunk = {
        "id": "agent-step",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "extra": {AGENT_STEP: event},
    }
    return "data: " + json.dumps(chunk, ensure_ascii=False)


class ReplyLines:
    """The client's SSE lines for one streamed reply of the model, as its chunks come.

    The model's thinking goes out as it comes, as ``reasoning_content``. Its
    text is held until it is clear it is the answer: ``HELD_TEXT_CHARS`` of
    it with no tool call, or the reply's end without one. When a tool call
    comes first, the text was the model saying what it is about to do, and
    goes out as thinking. ``reply`` is the reply put back together.
    """

    def __init__(self) -> None:
        self.reply = StreamedReply()
        self._held: list[tuple[dict, str]] = []
        self._held_chars = 0
        self._answering = False

    def add(self, chunk: dict) -> list[str]:
        """Take one chunk; return the lines to send for it."""
        choices = chunk.get("choices") or [{}]
        thought = reasoning_text((choices[0] if isinstance(choices[0], dict) else {}).get("delta") or {})
        text = self.reply.add(chunk)
        lines = [reasoning_line(chunk, thought)] if thought else []
        if self.reply.calls_tools:
            lines += self._release(as_answer=False)
            if text:
                lines.append(reasoning_line(chunk, text) if not self._answering else text_line(chunk, text))
        elif text and self._answering:
            lines.append(text_line(chunk, text))
        elif text:
            self._held.append((chunk, text))
            self._held_chars += len(text)
            if self._held_chars >= HELD_TEXT_CHARS:
                self._answering = True
                lines += self._release(as_answer=True)
        return lines

    def end(self) -> list[str]:
        """The lines still held when the reply ends: the answer, unless it called tools."""
        return self._release(as_answer=not self.reply.calls_tools)

    def _release(self, *, as_answer: bool) -> list[str]:
        held, self._held, self._held_chars = self._held, [], 0
        line = text_line if as_answer else reasoning_line
        return [line(chunk, text) for chunk, text in held]


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
    "HELD_TEXT_CHARS",
    "LEFT_OUT_RESULT",
    "MAX_CALLS_PER_ROUND",
    "ReplyLines",
    "StreamedReply",
    "assistant_turn",
    "call_signature",
    "describe_call",
    "finish_line",
    "leave_out_old_results",
    "reasoning_line",
    "sse_chunk",
    "step_line",
    "text_line",
    "tool_calls",
]
