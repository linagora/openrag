"""The chat completions take a conversation that ends with a message of the user,
or with the result of a tool call of the assistant that the client ran: the LLM
goes on from it (an agentic loop)."""

from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.root import Settings
from di.providers import get_config, get_partition_service, get_query_service
from fastapi import FastAPI


class RecordingQueryService:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    async def refresh_partition_configs(self) -> None:
        pass

    async def chat(self, **kwargs: Any) -> dict:
        self.calls["chat"] = kwargs
        return {"choices": [], "extra": {}}


class FakePartitionService:
    async def partition_exists(self, name: str) -> bool:
        return True


@pytest.fixture
def service() -> RecordingQueryService:
    return RecordingQueryService()


@pytest.fixture
def client(async_client_factory, monkeypatch, service):
    monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[current_user] = lambda: {"id": 2, "is_admin": False}
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: ["p"]
    app.dependency_overrides[get_query_service] = lambda: service
    app.dependency_overrides[get_partition_service] = FakePartitionService
    app.dependency_overrides[get_config] = lambda: Settings()
    return async_client_factory(app)


TOOL_TURN = [
    {"role": "user", "content": "Reply to the last email of Claire"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search_emails", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "2 emails: ..."},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openrag-p", None])
async def test_a_conversation_may_end_with_a_tool_result(client, service, model):
    body = {"messages": TOOL_TURN, "stream": False}
    if model:
        body["model"] = model
    async with client:
        response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200, response.text
    sent = service.calls["chat"]["payload"]["messages"]
    assert sent[-1] == TOOL_TURN[-1]
    assert sent[1]["tool_calls"][0]["id"] == "c1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        [{"role": "user", "content": ""}],
        [{"role": "user", "content": "hello"}, {"role": "tool", "content": "2 emails: ..."}],
        [{"role": "user", "content": "hello"}, {"role": "tool", "tool_call_id": "c1"}],
    ],
)
async def test_the_last_message_is_a_non_empty_user_message_or_a_tool_result(client, messages):
    async with client:
        response = await client.post("/v1/chat/completions", json={"model": "openrag-p", "messages": messages})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The last message must be a non-empty user message or a tool result with its tool_call_id"
    )


@pytest.mark.asyncio
async def test_a_tool_may_return_nothing(client, service):
    messages = [*TOOL_TURN[:-1], {"role": "tool", "tool_call_id": "c1", "content": ""}]
    async with client:
        response = await client.post("/v1/chat/completions", json={"model": "openrag-p", "messages": messages})

    assert response.status_code == 200, response.text
    assert service.calls["chat"]["payload"]["messages"][-1]["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        ({"model": "openrag-p"}, 400),
        ({"metadata": {"websearch": True}}, 400),
        ({}, 200),
    ],
)
async def test_only_a_search_needs_a_user_message(client, body, status_code):
    async with client:
        response = await client.post("/v1/chat/completions", json={**body, "messages": TOOL_TURN[1:]})

    assert response.status_code == status_code, response.text


@pytest.mark.asyncio
async def test_a_search_needs_the_latest_user_message_non_empty(client):
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": ""},
        *TOOL_TURN[1:],
    ]
    async with client:
        response = await client.post("/v1/chat/completions", json={"model": "openrag-p", "messages": messages})

    assert response.status_code == 400
    assert response.json()["detail"] == "A search needs the latest user message to be non-empty"
