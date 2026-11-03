"""A streamed chat answers 200 before it can fail, so a failure reaches the client
as an error event and the log level is the only part that follows the exception.
"""

from __future__ import annotations

import json

import pytest
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, ModelEndpointConfig
from core.config.root import Settings
from core.utils.exceptions import (
    InferenceError,
    InferenceTimeoutError,
    NotFoundError,
    OpenRAGError,
    VDBSearchError,
)
from di.providers import get_config, get_partition_service, get_query_service
from fastapi import FastAPI
from loguru import logger


class FailingQueryService:
    def __init__(self, exc: OpenRAGError) -> None:
        self.exc = exc

    async def refresh_partition_configs(self) -> None:
        pass

    async def chat_stream(self, **kwargs):
        raise self.exc
        yield  # pragma: no cover - makes this an async generator


class FakePartitionService:
    async def partition_exists(self, name: str) -> bool:
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "level"),
    [
        # The server failed.
        (InferenceError("LLM streaming error (503)", status_code=503), "ERROR"),
        (InferenceTimeoutError("LLM timed out"), "ERROR"),
        (VDBSearchError("Milvus search failed"), "ERROR"),
        # A provider refused OpenRag's call.
        (InferenceError("LLM streaming error (400)", status_code=400), "WARNING"),
        # The caller's own endpoint or request.
        (InferenceError("LLM streaming error (404)", status_code=404, caller_shaped=True), "INFO"),
        (NotFoundError("no such workspace"), "INFO"),
    ],
    ids=lambda v: getattr(v, "code", None) if isinstance(v, OpenRAGError) else None,
)
async def test_streamed_failure_logs_at_the_level_of_its_exception(
    exc: OpenRAGError, level: str, async_client_factory, monkeypatch
) -> None:
    monkeypatch.setattr(chat, "get_num_tokens", lambda: lambda text: len(text.split()))
    monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})
    settings = Settings()
    settings.models.llm["default"] = ModelEndpointConfig(
        endpoint="http://llm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: 16384}
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[current_user] = lambda: {"id": 2, "is_admin": False}
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: ["p"]
    app.dependency_overrides[get_query_service] = lambda: FailingQueryService(exc)
    app.dependency_overrides[get_partition_service] = FakePartitionService
    app.dependency_overrides[get_config] = lambda: settings

    captured: list[str] = []
    handler_id = logger.add(
        lambda m: captured.append(m.record["level"].name),
        level="DEBUG",
        filter=lambda r: r["message"] == "OpenRAG error during streaming",
    )
    body = {"model": "openrag-p", "messages": [{"role": "user", "content": "hello"}], "stream": True}
    try:
        async with async_client_factory(app) as client:
            response = await client.post("/v1/chat/completions", json=body)
    finally:
        logger.remove(handler_id)

    assert captured == [level]
    assert response.status_code == 200
    events = [line[len("data: ") :] for line in response.text.splitlines() if line.startswith("data: ")]
    assert json.loads(events[0])["error"]["code"] == exc.code
    assert events[-1] == "[DONE]"
