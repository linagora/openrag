"""Unit tests for ``core.chat.agent_trace``: recording an agent run into ``extra.agent_trace``."""

from __future__ import annotations

from core.chat.agent_trace import AgentTrace, CallRecord
from core.models.agent_trace import AgentTraceV1
from core.models.chunk import Chunk
from core.tools import ToolCall, ToolResult


def _trace(**overrides) -> AgentTrace:
    return AgentTrace(
        **{"request_id": "req", "profile": "default", "max_tool_rounds": 6, "max_prompt_tokens": 9000, **overrides}
    )


def _reply(*, calls: int = 0, finish_reason: str = "stop", usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": None if calls else "The answer."}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}"} for i in range(calls)]
    response: dict = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        response["usage"] = usage
    return response


_SEARCH = ToolCall("c0", "search_knowledge", '{"queries": ["notice period of the contract"]}')
_FOUND = ToolResult("c0", "search_knowledge", "2 excerpts, best match first.\n\n[Source 1]\nx\n---\n[Source 2]\ny")


def test_the_trace_has_each_step_in_the_order_it_ran():
    trace = _trace()
    trace.reply(
        "agent", round=1, duration_seconds=1.5, prompt_tokens=2400, response=_reply(calls=1, finish_reason="tool_calls")
    )
    trace.tools(round=1, duration_seconds=0.4, calls=[CallRecord(_SEARCH, _FOUND, duration_seconds=0.3)])
    trace.reply("agent", round=2, duration_seconds=0.8, prompt_tokens=3100, response=_reply())

    out = trace.finish(configuration_fingerprint="fp", total_seconds=3.0)

    assert AgentTraceV1.model_validate(out).model_dump(mode="json") == out
    assert out["schema_version"] == 1
    assert (out["request_id"], out["profile"], out["max_tool_rounds"], out["max_prompt_tokens"]) == (
        "req",
        "default",
        6,
        9000,
    )
    assert (out["rounds"], out["finalized"]) == (1, False)
    assert [(step["step"], step["round"]) for step in out["steps"]] == [("agent", 1), ("tools", 1), ("agent", 2)]
    first = out["steps"][0]
    assert (first["prompt_tokens"], first["finish_reason"], first["tool_calls"]) == (2400, "tool_calls", 1)
    assert out["steps"][2]["tool_calls"] == 0
    assert out["timings"] == {"total": 3.0}
    assert out["configuration_fingerprint"] == "fp"


def test_a_call_shows_what_the_model_asked_and_the_sources_it_was_shown():
    trace = _trace()
    trace.tools(round=1, duration_seconds=0.4, calls=[CallRecord(_SEARCH, _FOUND, duration_seconds=0.3)])

    (call,) = trace.finish(configuration_fingerprint="fp", total_seconds=1.0)["steps"][0]["calls"]

    assert call == {
        "id": "c0",
        "tool": "search_knowledge",
        "arguments": {"queries": ["notice period of the contract"]},
        "status": "ok",
        "summary": "2 excerpts, best match first.",
        "duration_seconds": 0.3,
        "sources": [1, 2],
        "retrieval_trace": None,
    }


def test_a_failed_call_is_an_error_and_one_answered_without_running_is_refused():
    broken = ToolCall("c1", "read_passage", "{not json")
    again = ToolCall("c2", "search_knowledge", _SEARCH.arguments)
    trace = _trace()
    trace.tools(
        round=2,
        duration_seconds=0.1,
        calls=[
            CallRecord(broken, ToolResult("c1", "read_passage", "Error: the arguments are not JSON.", is_error=True)),
            CallRecord(
                again,
                ToolResult("c2", "search_knowledge", "Error: you already made this call.", is_error=True),
                refused=True,
            ),
        ],
    )

    calls = trace.finish(configuration_fingerprint="fp", total_seconds=1.0)["steps"][0]["calls"]

    assert [(call["status"], call["summary"]) for call in calls] == [
        ("error", "Error: the arguments are not JSON."),
        ("refused", "Error: you already made this call."),
    ]
    assert calls[0]["arguments"] == "{not json", "arguments that don't parse are shown as the model sent them"
    assert calls[1]["duration_seconds"] is None


def test_a_call_that_searched_carries_its_retrieval_trace():
    trace = _trace()
    searched = trace.search(1, 0)
    searched.record_stage(
        "final",
        status="complete",
        candidates=searched.project_chunks(
            "final", [Chunk(id="k1", text="The notice period is three months.", metadata={})]
        ),
    )
    unused = trace.search(1, 1)
    trace.tools(
        round=1,
        duration_seconds=0.5,
        calls=[
            CallRecord(_SEARCH, _FOUND, duration_seconds=0.3, search=searched),
            CallRecord(
                ToolCall("c1", "find_files", "{}"), ToolResult("c1", "find_files", "No file matched."), search=unused
            ),
        ],
    )

    first, second = trace.finish(configuration_fingerprint="fp", total_seconds=1.0)["steps"][0]["calls"]

    retrieval = first["retrieval_trace"]
    assert retrieval["schema_version"] == 1
    assert retrieval["request_id"] == "req:round:1:call:1"
    assert retrieval["configuration_fingerprint"] == "fp"
    final = next(stage for stage in retrieval["stages"] if stage["name"] == "final")
    assert [candidate["id"] for candidate in final["candidates"]] == ["k1"]
    assert "three months" not in str(retrieval), "like classic's trace, no document text"
    assert second["retrieval_trace"] is None, "a call given a trace it didn't search with shows none"


def test_the_searches_of_a_run_share_one_budget_of_candidates():
    trace = _trace()
    first, second = trace.search(1, 0), trace.search(2, 0)
    remaining = second.diagnostics.remaining_candidates

    first.record_stage(
        "final", status="complete", candidates=first.project_chunks("final", [Chunk(id="k", text="t", metadata={})])
    )

    assert second.diagnostics.remaining_candidates == remaining - 1


def test_finalize_records_why_and_marks_the_run_finalized():
    trace = _trace(max_prompt_tokens=None)
    trace.reply(
        "finalize", round=7, duration_seconds=2.0, prompt_tokens=5000, response=_reply(), reason="no rounds left"
    )

    out = trace.finish(configuration_fingerprint="unavailable", total_seconds=2.5)

    assert out["finalized"] is True
    assert out["max_prompt_tokens"] is None
    assert out["steps"][0]["reason"] == "no rounds left"


def test_the_usage_the_endpoint_reported_is_kept_with_its_reasoning_tokens():
    trace = _trace()
    reported = {
        "prompt_tokens": 2000,
        "completion_tokens": 300,
        "total_tokens": 2300,
        "completion_tokens_details": {"reasoning_tokens": 250},
        "prompt_tokens_details": {"cached_tokens": 1000},
        "bogus": True,
    }
    trace.reply("agent", round=1, duration_seconds=1.0, prompt_tokens=2100, response=_reply(usage=reported))
    trace.reply(
        "agent", round=2, duration_seconds=1.0, prompt_tokens=2100, response=_reply(usage={"total_tokens": True})
    )

    first, second = trace.finish(configuration_fingerprint="fp", total_seconds=2.0)["steps"]

    assert first["usage"] == {
        "prompt_tokens": 2000,
        "completion_tokens": 300,
        "total_tokens": 2300,
        "reasoning_tokens": 250,
    }
    assert second["usage"] is None
