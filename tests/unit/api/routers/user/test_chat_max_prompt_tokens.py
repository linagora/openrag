"""The chat routes hand ``QueryService`` what the answering model's window leaves
for the prompt: its context size minus the output budget (``max_prompt_tokens``),
and report a prompt that doesn't fit in it as a 413.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY, ModelEndpointConfig
from core.config.root import Settings
from core.models.preset import PartitionConfig
from core.utils.exceptions import ContextWindowExceededError
from di.providers import get_config, get_partition_service, get_query_service
from fastapi import FastAPI
from services.orchestrators.query_service import QueryService

WINDOW = 16384


@pytest.fixture(autouse=True)
def _deterministic_token_counter(monkeypatch):
    monkeypatch.setattr(chat, "get_num_tokens", lambda: lambda text: len(text.split()))


class RecordingQueryService:
    """Records the keyword arguments each QueryService entry point is called with."""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    async def refresh_partition_configs(self) -> None:
        self.calls["refresh_partition_configs"] = {}

    def resolve_chat_request(self, _partitions):
        return SimpleNamespace(endpoint_name="default")

    async def chat(self, **kwargs: Any) -> dict:
        self.calls["chat"] = kwargs
        return {"choices": [], "extra": {}}

    async def chat_stream(self, **kwargs: Any):
        self.calls["chat_stream"] = kwargs
        yield "data: [DONE]\n\n"

    async def complete(self, **kwargs: Any) -> dict:
        self.calls["complete"] = kwargs
        return {"choices": [], "extra": {}}


class OverWindowQueryService:
    """Answers every request as one whose instructions and conversation overflow the window."""

    async def refresh_partition_configs(self) -> None:
        pass

    def resolve_chat_request(self, _partitions):
        return SimpleNamespace(endpoint_name="default")

    async def chat(self, **kwargs: Any) -> dict:
        raise ContextWindowExceededError(9000, 8000)

    async def chat_stream(self, **kwargs: Any):
        raise ContextWindowExceededError(9000, 8000)
        yield  # pragma: no cover - makes this an async generator

    async def complete(self, **kwargs: Any) -> dict:
        raise ContextWindowExceededError(9000, 8000)


class FakePartitionService:
    async def partition_exists(self, name: str) -> bool:
        return True


@pytest.fixture
def service() -> RecordingQueryService:
    return RecordingQueryService()


@pytest.fixture
def settings():
    settings = Settings(rag={"mode": "SimpleRag"})
    settings.models.llm["default"] = ModelEndpointConfig(
        endpoint="http://llm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: WINDOW}
    )
    return settings


@pytest.fixture
def make_client(async_client_factory, monkeypatch, settings):
    monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

    def _make(service):
        app = FastAPI()
        register_error_handlers(app)
        app.include_router(chat.router, prefix="/v1")
        app.dependency_overrides[current_user] = lambda: {"id": 2, "is_admin": False}
        app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: ["p"]
        app.dependency_overrides[get_query_service] = lambda: service
        app.dependency_overrides[get_partition_service] = FakePartitionService
        app.dependency_overrides[get_config] = lambda: settings
        return async_client_factory(app)

    return _make


@pytest.fixture
def client(make_client, service):
    return make_client(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stream", "entry_point"), [(False, "chat"), (True, "chat_stream")])
async def test_chat_completions_pass_the_window_minus_the_output_budget(client, service, stream, entry_point):
    body = {
        "model": "openrag-p",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 2048,
        "stream": stream,
    }
    async with client:
        response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    assert service.calls[entry_point]["max_prompt_tokens"] == WINDOW - 2048
    assert list(service.calls) == ["refresh_partition_configs", entry_point]


@pytest.mark.asyncio
async def test_completions_pass_the_window_minus_the_output_budget(client, service):
    body = {"model": "openrag-p", "prompt": "hello", "max_tokens": 2048}
    async with client:
        response = await client.post("/v1/completions", json=body)

    assert response.status_code == 200
    assert service.calls["complete"]["max_prompt_tokens"] == WINDOW - 2048
    assert list(service.calls) == ["refresh_partition_configs", "complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["chat", "chat_stream", "complete"])
async def test_budget_and_generation_use_the_same_model_after_cache_refresh(
    make_client, settings, monkeypatch, entry_point
):
    defaults = {"before": 73, "after": 119}
    for name, window in [("before", 4096), ("after", 8192)]:
        settings.models.llm[name] = ModelEndpointConfig(
            endpoint=f"http://{name}:8000/v1",
            extra={LLM_CONTEXT_SIZE_KEY: window, LLM_OUTPUT_TOKENS_KEY: defaults[name]},
        )
    before = PartitionConfig(name="p", indexation={}, retrieval={}, chat_llm="before")
    after = before.model_copy(update={"chat_llm": "after"})
    settings.partitions["p"] = before
    calls = []

    def make_llm(name):
        def record(kwargs):
            calls.append((name, kwargs["max_tokens"]))

        async def answer(_messages, **kwargs):
            record(kwargs)
            return {"choices": [{"message": {"content": "Answer"}}]}

        async def stream(_messages, **kwargs):
            record(kwargs)
            yield 'data: {"choices":[{"delta":{"content":"Answer"},"finish_reason":"stop"}]}\n\n'
            yield "data: [DONE]\n\n"

        async def complete(_prompt, **kwargs):
            record(kwargs)
            return {"choices": [{"text": "Answer"}]}

        return SimpleNamespace(chat=answer, stream_chat=stream, generate=complete)

    llms = {name: make_llm(name) for name in defaults}
    service = QueryService(
        retrieval_service=SimpleNamespace(
            refresh_partition_configs=AsyncMock(), retrieve_multi=AsyncMock(return_value=[])
        ),
        llm=llms["before"],
        config=settings,
        web_search_service=SimpleNamespace(max_tokens=1000),
        workspace_service=None,
        prompt_service=SimpleNamespace(resolve_prompt=AsyncMock(return_value="Answer using {context}")),
        llm_factory=llms.__getitem__,
    )
    prepare_method = "_prepare_completions" if entry_point == "complete" else "_prepare_chat"
    prepare = getattr(service, prepare_method)
    prompt_budgets = []

    async def record_prompt_budget(*args, **kwargs):
        prompt_budgets.append(kwargs["max_prompt_tokens"])
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(service, prepare_method, record_prompt_budget)

    def count_and_refresh(text):
        settings.partitions["p"] = after
        return len(text.split())

    monkeypatch.setattr(chat, "get_num_tokens", lambda: count_and_refresh)
    if entry_point == "complete":
        path, body = "/v1/completions", {"prompt": "What is the plan?"}
    else:
        path = "/v1/chat/completions"
        body = {"messages": [{"role": "user", "content": "What is the plan?"}], "stream": entry_point == "chat_stream"}

    async with make_client(service) as client:
        for _ in range(2):
            response = await client.post(path, json={**body, "model": "openrag-p"})
            assert response.status_code == 200
            assert "Answer" in response.text

    assert calls == [("before", 73), ("after", 119)]
    assert prompt_budgets == [4096 - 73, 8192 - 119]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/completions", {"model": "openrag-p", "messages": [{"role": "user", "content": "hello"}]}),
        ("/v1/completions", {"model": "openrag-p", "prompt": "hello"}),
    ],
)
async def test_a_prompt_over_the_window_is_a_413(make_client, path, body):
    async with make_client(OverWindowQueryService()) as client:
        response = await client.post(path, json=body)

    assert response.status_code == 413
    assert response.json()["detail"].startswith("[CONTEXT_WINDOW_EXCEEDED]")


@pytest.mark.asyncio
async def test_a_streamed_prompt_over_the_window_is_an_error_event(make_client):
    """The stream's headers are sent before the prompt is built, so the 413 comes as an error event."""
    body = {"model": "openrag-p", "messages": [{"role": "user", "content": "hello"}], "stream": True}
    async with make_client(OverWindowQueryService()) as client:
        response = await client.post("/v1/chat/completions", json=body)

    events = [line[len("data: ") :] for line in response.text.splitlines() if line.startswith("data: ")]
    assert json.loads(events[0])["error"]["code"] == "CONTEXT_WINDOW_EXCEEDED"
    assert events[-1] == "[DONE]"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["initialization", "invocation"])
@pytest.mark.parametrize("model", [None, "openrag-p"])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "private question"}]}),
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "private question"}], "stream": True}),
        ("/v1/completions", {"prompt": "private question"}),
    ],
)
async def test_counter_failure_returns_503_before_processing(
    client, service, monkeypatch, failure_stage, model, path, body
):
    def fail_counting(*_args):
        raise RuntimeError("secret tokenizer details")

    monkeypatch.setattr(
        chat, "get_num_tokens", fail_counting if failure_stage == "initialization" else lambda: fail_counting
    )
    async with client:
        response = await client.post(path, json={**body, "model": model})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"].startswith(
        "[TOKEN_VALIDATION_UNAVAILABLE]: Unable to validate request token limit"
    )
    assert "secret" not in response.text
    assert "private question" not in response.text
    assert "data:" not in response.text
    assert not {"chat", "chat_stream", "complete"}.intersection(service.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}], "stream": True}),
        ("/v1/completions", {"prompt": "hello"}),
    ],
)
async def test_over_budget_request_returns_413_before_processing(client, service, path, body):
    async with client:
        response = await client.post(path, json={**body, "model": "openrag-p", "max_tokens": WINDOW})

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert "Request exceeds maximum token limit" in response.json()["detail"]
    assert "Requested output: 16384 tokens" in response.json()["detail"]
    assert not {"chat", "chat_stream", "complete"}.intersection(service.calls)
