"""Unit tests for ``core.tools.runner.run_tool``: whatever goes wrong, the model reads why."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import pytest
from core.chat.scope import Scope
from core.tools import ToolCall, ToolContext, ToolError, ToolRegistry, run_tool, tool
from pydantic import Field


class Toolset:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @tool("echo", "Echo the text")
    async def echo(self, ctx: ToolContext, text: str, times: Annotated[int, Field(ge=1, le=3)] = 1) -> str:
        self.calls.append({"user": ctx.user["id"], "text": text, "times": times})
        return text * times

    @tool("info", "A dict result")
    async def info(self, ctx: ToolContext) -> dict:
        return {"name": "café", "size": 3}

    @tool("missing", "Raises a ToolError")
    async def missing(self, ctx: ToolContext, file_id: str) -> str:
        raise ToolError(f"No file '{file_id}' in this scope.")

    @tool("broken", "Raises an unexpected error")
    async def broken(self, ctx: ToolContext) -> str:
        raise RuntimeError("password=hunter2 in the connection string")

    @tool("slow", "Never answers in time", timeout=0.01)
    async def slow(self, ctx: ToolContext) -> str:
        await asyncio.sleep(10)
        return "late"

    @tool("cancelled", "Is cancelled")
    async def cancelled(self, ctx: ToolContext) -> str:
        raise asyncio.CancelledError


@pytest.fixture
def toolset() -> Toolset:
    return Toolset()


async def _run(toolset: Toolset, name: str, arguments: str):
    ctx = ToolContext(user={"id": 7}, scope=Scope(partitions=["p"]))
    return await run_tool(ToolRegistry.from_toolsets(toolset), ToolCall("call-1", name, arguments), ctx)


async def test_a_valid_call_runs_with_the_context_and_its_validated_arguments(toolset):
    result = await _run(toolset, "echo", json.dumps({"text": "ab", "times": 2}))

    assert (result.content, result.is_error) == ("abab", False)
    assert toolset.calls == [{"user": 7, "text": "ab", "times": 2}]
    assert result.as_message() == {"role": "tool", "tool_call_id": "call-1", "content": "abab"}


async def test_a_non_text_result_is_sent_as_json(toolset):
    result = await _run(toolset, "info", "")

    assert json.loads(result.content) == {"name": "café", "size": 3}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("{not json", "the arguments are not valid JSON"),
        ("[1, 2]", "the arguments must be a JSON object"),
        ("{}", "invalid arguments: text: Field required"),
        ('{"text": "a", "times": 9}', "invalid arguments: times: Input should be less than or equal to 3"),
        ('{"text": "a", "partition": "hr"}', "invalid arguments: partition: Extra inputs are not permitted"),
    ],
)
async def test_bad_arguments_are_an_error_the_model_reads_and_the_tool_never_runs(toolset, arguments, expected):
    result = await _run(toolset, "echo", arguments)

    assert result.is_error
    assert expected in result.content
    assert toolset.calls == []


async def test_an_unknown_tool_lists_the_ones_there_are(toolset):
    result = await _run(toolset, "delete_everything", "{}")

    assert result.is_error
    assert "there is no tool 'delete_everything'" in result.content
    assert "echo, info" in result.content


async def test_a_tool_error_is_passed_on_as_written(toolset):
    result = await _run(toolset, "missing", '{"file_id": "x"}')

    assert (result.content, result.is_error) == ("Error: No file 'x' in this scope.", True)


async def test_an_unexpected_failure_hides_its_message_from_the_model(toolset):
    result = await _run(toolset, "broken", "{}")

    assert (result.content, result.is_error) == ("Error: 'broken' failed.", True)


async def test_a_call_over_its_timeout_is_an_error(toolset):
    result = await _run(toolset, "slow", "{}")

    assert (result.content, result.is_error) == ("Error: 'slow' did not answer within 0.01 seconds.", True)


async def test_a_cancellation_is_not_swallowed(toolset):
    with pytest.raises(asyncio.CancelledError):
        await _run(toolset, "cancelled", "{}")
