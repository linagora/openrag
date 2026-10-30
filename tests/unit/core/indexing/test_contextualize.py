from __future__ import annotations

import asyncio
import gc

import pytest
from core.indexing import contextualize as contextualize_module
from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk


class DictLLM:
    async def chat(self, messages, **kwargs):
        return {"choices": [{"message": {"content": "document-level context"}}]}


@pytest.mark.asyncio
async def test_contextualizer_accepts_openai_style_chat_response():
    contextualizer = ChunkContextualizer(DictLLM(), "System prompt")

    result = await contextualizer.contextualize([Chunk(id="c1", text="chunk body", partition="p")])

    assert result[0].context == "document-level context"
    assert result[0].content == "chunk body"


class _GatingLLM:
    """LLM stub that records whether the LLM gate was held during ``chat``."""

    def __init__(self, gate_held: list[bool]):
        self._gate_held = gate_held

    async def chat(self, messages, **kwargs):
        self._gate_held.append(_TrackingGate.depth > 0)
        return {"choices": [{"message": {"content": "ctx"}}]}


class _TrackingGate:
    """Async context manager standing in for the distributed LLM semaphore."""

    depth = 0

    async def __aenter__(self):
        type(self).depth += 1
        return self

    async def __aexit__(self, *exc):
        type(self).depth -= 1


@pytest.mark.asyncio
async def test_contextualizer_holds_llm_semaphore_around_chat():
    _TrackingGate.depth = 0
    gate_held: list[bool] = []
    contextualizer = ChunkContextualizer(_GatingLLM(gate_held), "System prompt", llm_semaphore=_TrackingGate())

    await contextualizer.contextualize([Chunk(id="c1", text="chunk body", partition="p")])

    # The injected gate was entered for the chat call and released afterwards.
    assert gate_held == [True]
    assert _TrackingGate.depth == 0


@pytest.mark.asyncio
async def test_contextualizer_reports_provider_failure_while_returning_base_chunks():
    class FailingLLM:
        async def chat(self, messages, **kwargs):
            raise RuntimeError("context provider unavailable")

    failures: list[Exception] = []
    contextualizer = ChunkContextualizer(FailingLLM(), "System prompt")

    result = await contextualizer.contextualize(
        [Chunk(id="c1", text="chunk body", partition="p")],
        on_failure=failures.append,
    )

    assert result[0].context == ""
    assert result[0].content == "chunk body"
    assert len(failures) == 1
    assert str(failures[0]) == "context provider unavailable"


@pytest.mark.asyncio
async def test_contextualizer_reports_empty_provider_response_as_degraded():
    class EmptyLLM:
        async def chat(self, messages, **kwargs):
            return {"choices": [{"message": {"content": ""}}]}

    failures: list[Exception] = []
    contextualizer = ChunkContextualizer(EmptyLLM(), "System prompt")

    result = await contextualizer.contextualize(
        [Chunk(id="c1", text="chunk body", partition="p")],
        on_failure=failures.append,
    )

    assert result[0].content == "chunk body"
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)


async def _unretrieved_task_errors(coro) -> list[str]:
    """Await *coro*, then return the "Task exception was never retrieved"
    contexts asyncio reports once the leftover tasks are garbage collected."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    recorded: list[str] = []

    def handler(_loop, ctx):
        if "never retrieved" in ctx["message"]:
            recorded.append(f"{ctx['message']}: {ctx.get('future')!r}")

    loop.set_exception_handler(handler)
    try:
        await coro
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    return recorded


@pytest.mark.asyncio
async def test_contextualizer_leaves_no_unretrieved_task_exception_when_several_chunks_fail(monkeypatch):
    """#1299: tqdm.gather re-raises the first failure and abandons the other
    wrapper tasks, so a second failing chunk logs "Task exception was never
    retrieved". The semaphore failing on entry is outside _generate_context's
    try, so it reaches the gather."""

    class FailingGate:
        async def __aenter__(self):
            raise RuntimeError("llm semaphore unavailable")

        async def __aexit__(self, *exc):
            return None

    # pytest's log capture keeps each LogRecord, and the record's ``exc`` arg
    # holds a traceback that keeps the abandoned tasks alive past gc.collect().
    monkeypatch.setattr(contextualize_module.logger, "disabled", True)
    chunks = [Chunk(id=f"c{i}", text=f"chunk {i}", partition="p") for i in range(3)]
    # Record only the type: the exception's traceback would keep the abandoned
    # tasks alive past the gc.collect() below.
    failures: list[type[Exception]] = []
    results: list[list[Chunk]] = []
    contextualizer = ChunkContextualizer(DictLLM(), "System prompt", batch_size=4, llm_semaphore=FailingGate())

    async def run():
        results.append(await contextualizer.contextualize(chunks, on_failure=lambda exc: failures.append(type(exc))))

    recorded = await _unretrieved_task_errors(run())

    assert results == [chunks]
    assert failures == [RuntimeError]
    assert recorded == []
