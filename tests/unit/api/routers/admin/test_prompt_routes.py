"""Transport tests for the prompt library router.

Service behaviour is covered by the PromptService unit tests; here we assert
request→service forwarding, response shaping, schema validation (422), and that
service-raised domain errors map to the right status via the shared handlers.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import require_admin
from api.error_handlers import register_error_handlers
from api.routers.admin import prompts
from core.models.prompt import Prompt
from core.utils.exceptions import NotFoundError, ValidationError
from di.providers import get_prompt_service
from fastapi import FastAPI


def _prompt(**overrides: Any) -> Prompt:
    data = {"prompt_type": "sys_prompt", "name": "p", "content": "body", "is_default": False}
    data.update(overrides)
    return Prompt(**data)


class FakePromptService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    async def create_prompt(self, *, prompt_type: str, name: str, content: str, is_default: bool = False) -> Prompt:
        self.calls.append(
            ("create", {"prompt_type": prompt_type, "name": name, "content": content, "is_default": is_default})
        )
        return _prompt(prompt_type=prompt_type, name=name, content=content, is_default=is_default)

    async def list_prompts(self, *, prompt_type=None, offset=0, limit=100) -> list[Prompt]:
        self.calls.append(("list", {"prompt_type": prompt_type, "offset": offset, "limit": limit}))
        return [_prompt(prompt_type=prompt_type or "sys_prompt")]

    async def get_prompt(self, prompt_id: str) -> Prompt:
        if self.error:
            raise self.error
        self.calls.append(("get", {"prompt_id": prompt_id}))
        return _prompt(id=prompt_id)

    async def update_prompt(self, prompt_id: str, **fields: Any) -> Prompt:
        self.calls.append(("update", {"prompt_id": prompt_id, **fields}))
        return _prompt(id=prompt_id, **{k: v for k, v in fields.items() if k in ("name", "content", "is_default")})

    async def set_default(self, prompt_id: str) -> Prompt:
        self.calls.append(("set_default", {"prompt_id": prompt_id}))
        return _prompt(id=prompt_id, is_default=True)

    async def delete_prompt(self, prompt_id: str) -> None:
        if self.error:
            raise self.error
        self.calls.append(("delete", {"prompt_id": prompt_id}))


def _build_app(service: FakePromptService) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(prompts.router, prefix="/prompts")
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "is_admin": True}
    app.dependency_overrides[get_prompt_service] = lambda: service
    return app


pytestmark = pytest.mark.asyncio


class TestLibraryRoutes:
    async def test_create_forwards_and_returns_201(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.post(
                "/prompts/",
                json={"prompt_type": "sys_prompt", "name": "greet", "content": "hi", "is_default": True},
            )
        assert resp.status_code == 201
        assert resp.json()["prompt_type"] == "sys_prompt"
        assert svc.calls == [
            ("create", {"prompt_type": "sys_prompt", "name": "greet", "content": "hi", "is_default": True})
        ]

    async def test_create_rejects_empty_content(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.post("/prompts/", json={"prompt_type": "sys_prompt", "name": "blank", "content": "   "})
        assert resp.status_code == 422
        assert svc.calls == []

    async def test_create_allows_blank_asr_content(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.post(
                "/prompts/",
                json={"prompt_type": "asr_transcription", "name": "native", "content": "   "},
            )
        assert resp.status_code == 201
        assert svc.calls == [
            ("create", {"prompt_type": "asr_transcription", "name": "native", "content": "", "is_default": False})
        ]

    async def test_create_rejects_unknown_type(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.post("/prompts/", json={"prompt_type": "bogus", "content": "x"})
        assert resp.status_code == 422

    async def test_list_forwards_filters(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.get("/prompts/?prompt_type=sys_prompt&offset=2&limit=5")
        assert resp.status_code == 200
        assert resp.json()[0]["prompt_type"] == "sys_prompt"
        assert svc.calls == [("list", {"prompt_type": "sys_prompt", "offset": 2, "limit": 5})]

    async def test_get_missing_maps_to_404(self, async_client_factory):
        svc = FakePromptService()
        svc.error = NotFoundError("Prompt 'x' not found.")
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.get("/prompts/x")
        assert resp.status_code == 404

    async def test_patch_forwards_only_set_fields(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.patch("/prompts/pid", json={"content": "new", "is_default": True})
        assert resp.status_code == 200
        assert svc.calls == [("update", {"prompt_id": "pid", "content": "new", "is_default": True})]

    async def test_patch_empty_body_is_422(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.patch("/prompts/pid", json={})
        assert resp.status_code == 422

    async def test_set_default(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.put("/prompts/pid/default")
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True
        assert svc.calls == [("set_default", {"prompt_id": "pid"})]

    async def test_delete_returns_204(self, async_client_factory):
        svc = FakePromptService()
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.delete("/prompts/pid")
        assert resp.status_code == 204
        assert svc.calls == [("delete", {"prompt_id": "pid"})]

    async def test_delete_default_maps_to_422(self, async_client_factory):
        svc = FakePromptService()
        svc.error = ValidationError("Cannot delete the default 'sys_prompt' prompt.")
        async with async_client_factory(_build_app(svc)) as client:
            resp = await client.delete("/prompts/pid")
        assert resp.status_code == 422
