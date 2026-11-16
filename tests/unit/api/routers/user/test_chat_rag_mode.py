"""``metadata.rag_mode`` on the chat and text-completion endpoints: ``classic`` and ``agentic``, for chat."""

from __future__ import annotations

from typing import Any

import pytest
from api.dependencies import model_limits
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.root import Settings
from di.providers import get_chat_service, get_config, get_partition_service
from fastapi import FastAPI


class RecordingChatService:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    async def refresh_partition_configs(self) -> None:
        pass

    async def chat(self, **kwargs: Any) -> dict:
        self.calls["chat"] = kwargs
        return {"choices": [], "extra": {}}

    async def chat_stream(self, **kwargs: Any):
        self.calls["chat_stream"] = kwargs
        yield "data: [DONE]\n\n"

    async def complete(self, **kwargs: Any) -> dict:
        self.calls["complete"] = kwargs
        return {"choices": [], "extra": {}}


class FakePartitionService:
    async def partition_exists(self, name: str) -> bool:
        return True


@pytest.fixture
def service() -> RecordingChatService:
    return RecordingChatService()


@pytest.fixture
def client(async_client_factory, monkeypatch, service):
    monkeypatch.setattr(model_limits, "_max_model_tokens_by_name", {})
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[current_user] = lambda: {"id": 2, "is_admin": False}
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: ["p"]
    app.dependency_overrides[get_chat_service] = lambda: service
    app.dependency_overrides[get_partition_service] = FakePartitionService
    app.dependency_overrides[get_config] = lambda: Settings()
    return async_client_factory(app)


def _chat(rag_mode: object, model: str | None = "openrag-p") -> dict:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "notice period?"}], "metadata": {}}
    if rag_mode is not ...:
        body["metadata"]["rag_mode"] = rag_mode
    if model is not None:
        body["model"] = model
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_mode", [..., None, "classic", "agentic"])
async def test_both_modes_are_accepted(client, service, rag_mode):
    async with client:
        response = await client.post("/v1/chat/completions", json=_chat(rag_mode))

    assert response.status_code == 200, response.text
    assert service.calls["chat"]["partitions"] == ["p"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openrag-p", None])
async def test_any_other_mode_is_refused_before_anything_runs(client, service, model):
    """Refused in direct LLM mode too, where the mode is otherwise ignored: a typo never passes silently."""
    async with client:
        response = await client.post("/v1/chat/completions", json=_chat("simple", model))

    assert response.status_code == 422
    assert "rag_mode must be one of: classic, agentic" in response.text
    assert service.calls == {}


@pytest.mark.asyncio
async def test_an_agentic_request_streams(client, service):
    async with client:
        response = await client.post("/v1/chat/completions", json={**_chat("agentic"), "stream": True})

    assert response.status_code == 200, response.text
    assert service.calls["chat_stream"]["payload"]["metadata"]["rag_mode"] == "agentic"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rag_mode", "sent", "forwarded"),
    [
        ("agentic", {}, None),  # no temperature: the agent profile's applies
        ("agentic", {"temperature": 0.9}, 0.9),
        ("classic", {}, 0.3),  # classic keeps the API's default
    ],
)
async def test_an_agentic_request_forwards_only_a_temperature_it_sent(client, service, rag_mode, sent, forwarded):
    async with client:
        response = await client.post("/v1/chat/completions", json={**_chat(rag_mode), **sent})

    assert response.status_code == 200, response.text
    assert service.calls["chat"]["payload"].get("temperature") == forwarded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}, "runs its own tools"),
        ({"metadata": {"rag_mode": "agentic", "websearch": True}}, "doesn't search the web"),
        ({"response_format": {"type": "json_object"}}, "answers in text"),
    ],
)
async def test_an_agentic_request_asking_for_what_the_agent_doesnt_do_is_refused(client, service, change, message):
    async with client:
        response = await client.post("/v1/chat/completions", json={**_chat("agentic"), **change})

    assert response.status_code == 422
    assert message in response.text
    assert service.calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("rag_mode", "status_code"), [("classic", 200), ("agentic", 422)])
async def test_text_completions_check_the_mode_too(client, service, rag_mode, status_code):
    body = {"model": "openrag-p", "prompt": "notice period?", "metadata": {"rag_mode": rag_mode}}
    async with client:
        response = await client.post("/v1/completions", json=body)

    assert response.status_code == status_code, response.text
    assert ("complete" in service.calls) is (status_code == 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_mode", ["classic", "agentic"])
@pytest.mark.parametrize(("sent", "forwarded"), [({}, None), ({"reasoning_effort": "low"}, "low")])
async def test_a_reasoning_level_is_forwarded_only_when_the_request_sends_one(
    client, service, rag_mode, sent, forwarded
):
    async with client:
        response = await client.post("/v1/chat/completions", json={**_chat(rag_mode), **sent})

    assert response.status_code == 200, response.text
    assert service.calls["chat"]["payload"].get("reasoning_effort") == forwarded


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["max", "HIGH", ""])
async def test_a_reasoning_level_that_is_not_openai_s_is_refused(client, service, level):
    async with client:
        response = await client.post("/v1/chat/completions", json={**_chat("classic"), "reasoning_effort": level})

    assert response.status_code == 422
    assert "reasoning_effort" in response.text
    assert service.calls == {}
