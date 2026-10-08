"""The chat routes hand ``QueryService`` what the answering model's window leaves
for the prompt: its context size minus the output budget (``max_prompt_tokens``),
and report a prompt that doesn't fit in it as a 413.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, ModelEndpointConfig
from core.config.root import Settings
from core.utils.exceptions import ContextWindowExceededError
from di.providers import get_config, get_partition_service, get_query_service
from fastapi import FastAPI

WINDOW = 16384


class RecordingQueryService:
    """Records the keyword arguments each QueryService entry point is called with."""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    async def refresh_partition_configs(self) -> None:
        self.calls["refresh_partition_configs"] = {}

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
def make_client(async_client_factory, monkeypatch):
    monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})
    settings = Settings()
    settings.models.llm["default"] = ModelEndpointConfig(
        endpoint="http://llm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: WINDOW}
    )

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
