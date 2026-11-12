"""``metadata.rag_mode`` on the chat and text-completion endpoints: only ``classic`` is available."""

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

    async def complete(self, **kwargs: Any) -> dict:
        self.calls["complete"] = kwargs
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


def _chat(rag_mode: object, model: str | None = "openrag-p") -> dict:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "notice period?"}], "metadata": {}}
    if rag_mode is not ...:
        body["metadata"]["rag_mode"] = rag_mode
    if model is not None:
        body["model"] = model
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_mode", [..., None, "classic"])
async def test_classic_is_accepted(client, service, rag_mode):
    async with client:
        response = await client.post("/v1/chat/completions", json=_chat(rag_mode))

    assert response.status_code == 200, response.text
    assert service.calls["chat"]["partitions"] == ["p"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rag_mode", "message"),
    [("agentic", "rag_mode 'agentic' is not available yet"), ("simple", "rag_mode must be one of: classic")],
)
@pytest.mark.parametrize("model", ["openrag-p", None])
async def test_any_other_mode_is_refused_before_anything_runs(client, service, rag_mode, message, model):
    """Refused in direct LLM mode too, where the mode is otherwise ignored: a typo never passes silently."""
    async with client:
        response = await client.post("/v1/chat/completions", json=_chat(rag_mode, model))

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
