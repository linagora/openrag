"""Running one tool call the model asked for, so that whatever goes wrong the model reads why.

The model's arguments are untrusted: they are parsed and validated against
the tool's parameters before it runs. A bad call, a tool error, a timeout or
an unexpected failure all come back as the tool's answer, marked as an error,
never as an exception that ends the agent's run. Only a cancellation (the
client went away) propagates.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.tools.spec import ToolError
from core.utils.logging import get_logger
from pydantic import ValidationError

if TYPE_CHECKING:
    from core.tools.context import ToolContext
    from core.tools.spec import ToolRegistry

logger = get_logger()


@dataclass(frozen=True)
class ToolCall:
    """A tool call as the model sent it; *arguments* is its raw JSON object."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResult:
    """What the model reads back from a tool call."""

    call_id: str
    name: str
    content: str
    is_error: bool = False

    def as_message(self) -> dict[str, Any]:
        """The OpenAI ``tool`` message answering the call."""
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content}


async def run_tool(registry: ToolRegistry, call: ToolCall, ctx: ToolContext) -> ToolResult:
    """Run *call* with the tool of that name in *registry*, within the tool's timeout."""

    def failed(message: str) -> ToolResult:
        return ToolResult(call.id, call.name, f"Error: {message}", is_error=True)

    spec = registry.get(call.name)
    if spec is None:
        return failed(f"there is no tool '{call.name}'. The tools are: {', '.join(registry.names)}.")
    try:
        arguments = json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return failed("the arguments are not valid JSON.")
    if not isinstance(arguments, dict):
        return failed("the arguments must be a JSON object.")
    try:
        params = spec.params.model_validate(arguments)
    except ValidationError as e:
        return failed(f"invalid arguments: {_describe(e)}.")

    try:
        result = await asyncio.wait_for(
            spec.handler(ctx, **{name: getattr(params, name) for name in type(params).model_fields}),
            spec.timeout,
        )
    except ToolError as e:
        return failed(e.message)
    except TimeoutError:
        logger.bind(tool=call.name, timeout=spec.timeout).warning("Tool call timed out")
        return failed(f"'{call.name}' did not answer within {spec.timeout:g} seconds.")
    except Exception:
        # What went wrong is for the logs: an exception message can carry
        # internals the model, and through it the user, must not see.
        logger.bind(tool=call.name).exception("Tool call failed")
        return failed(f"'{call.name}' failed.")

    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return ToolResult(call.id, call.name, content)


def _describe(error: ValidationError) -> str:
    """Each invalid argument and why, in one line the model can act on."""
    problems = []
    for item in error.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in item["loc"]) or "arguments"
        problems.append(f"{where}: {item['msg']}")
    return "; ".join(problems)


__all__ = ["ToolCall", "ToolResult", "run_tool"]
