from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import require_admin
from api.routers.admin import model_endpoints, presets
from di.providers import get_model_endpoint_service, get_preset_service
from fastapi import FastAPI


def _model_endpoint_row(**overrides: Any) -> dict[str, Any]:
    """Build a model endpoint response row for router tests."""
    row = {
        "name": "default",
        "model_type": "llm",
        "endpoint": "http://llm:8000/v1",
        "model_name": "mistral",
        "batch_size": 32,
        "timeout": 30.0,
        "extra": {},
        "is_default": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _preset_row(**overrides: Any) -> dict[str, Any]:
    """Build a preset response row for router tests."""
    row = {
        "name": "default",
        "preset_type": "retrieval",
        "config": {"type": "single", "top_k": 50},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakeModelEndpointService:
    """Fake model endpoint service that records router calls."""

    def __init__(self) -> None:
        """Initialize the call log."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.endpoint_extra: dict[str, Any] = {}

    async def create_model_endpoint(self, row: Any) -> dict[str, Any]:
        """Record endpoint creation (from a ModelEndpointRow) and echo a row."""
        payload = row.model_dump(exclude={"created_at", "updated_at"})
        self.calls.append(("create", payload))
        return _model_endpoint_row(**payload)

    async def list_model_endpoints(self, model_type: str | None = None) -> list[dict[str, Any]]:
        """Record endpoint listing with the optional type filter."""
        self.calls.append(("list", {"model_type": model_type}))
        return [_model_endpoint_row(model_type=model_type or "llm")]

    async def get_model_endpoint(self, name: str, model_type: str) -> Any:
        """Record a single endpoint lookup."""
        from core.config.model_endpoints import ModelEndpointRow

        self.calls.append(("get", {"name": name, "model_type": model_type}))
        return ModelEndpointRow(**_model_endpoint_row(name=name, model_type=model_type, extra=self.endpoint_extra))

    async def update_model_endpoint(self, name: str, model_type: str, **fields: Any) -> dict[str, Any]:
        """Record endpoint updates and echo the merged response row."""
        self.calls.append(("update", {"name": name, "model_type": model_type, **fields}))
        return _model_endpoint_row(**{"name": name, "model_type": model_type, **fields})

    async def delete_model_endpoint(self, name: str, model_type: str) -> None:
        """Record endpoint deletion."""
        self.calls.append(("delete", {"name": name, "model_type": model_type}))

    async def set_default(self, model_type: str, name: str) -> None:
        """Record default promotion."""
        self.calls.append(("set_default", {"name": name, "model_type": model_type}))

    async def validate_endpoint(
        self,
        url: str,
        model_name: str | None = None,
        *,
        api_key: str | None = None,
        model_type: str | None = None,
    ) -> dict[str, Any]:
        """Record endpoint validation."""
        self.calls.append(
            ("validate", {"url": url, "model_type": model_type, "model_name": model_name, "api_key": api_key})
        )
        return {
            "reachable": True,
            "model_found": True,
            "models_served": ["mistral"],
            "transcription_supported": model_type == "stt",
            "detail": None,
        }


class FakePresetService:
    """Fake preset service that records router calls."""

    def __init__(self) -> None:
        """Initialize the call log."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_preset(self, name: str, preset_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Record preset creation (from unpacked kwargs) and echo a row."""
        payload = {"name": name, "preset_type": preset_type, "config": config}
        self.calls.append(("create", payload))
        return _preset_row(**payload)

    async def list_presets(self, preset_type: str | None = None) -> list[dict[str, Any]]:
        """Record preset listing with the optional type filter."""
        self.calls.append(("list", {"preset_type": preset_type}))
        return [_preset_row(preset_type=preset_type or "retrieval")]

    async def get_preset(self, name: str, preset_type: str) -> dict[str, Any]:
        """Record a single preset lookup."""
        self.calls.append(("get", {"name": name, "preset_type": preset_type}))
        return _preset_row(name=name, preset_type=preset_type)

    async def update_preset(self, name: str, preset_type: str, **fields: Any) -> dict[str, Any]:
        """Record preset updates and echo the merged response row."""
        self.calls.append(("update", {"name": name, "preset_type": preset_type, **fields}))
        return _preset_row(**{"name": name, "preset_type": preset_type, **fields})

    async def delete_preset(self, name: str, preset_type: str) -> None:
        """Record preset deletion."""
        self.calls.append(("delete", {"name": name, "preset_type": preset_type}))


def _build_app(
    *,
    model_service: FakeModelEndpointService | None = None,
    preset_service: FakePresetService | None = None,
) -> FastAPI:
    """Build a small app with Phase 14 routers and fake dependencies."""
    app = FastAPI()
    app.include_router(model_endpoints.router, prefix="/model-endpoints")
    app.include_router(presets.router, prefix="/presets")
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "is_admin": True}
    if model_service is not None:
        app.dependency_overrides[get_model_endpoint_service] = lambda: model_service
    if preset_service is not None:
        app.dependency_overrides[get_preset_service] = lambda: preset_service
    return app


@pytest.mark.asyncio
async def test_create_model_endpoint_normalizes_payload(async_client_factory):
    """Model endpoint creation should pass normalized schema data to the service."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={
                "name": " default ",
                "model_type": "llm",
                "endpoint": " http://llm:8000/v1/ ",
                "model_name": "mistral",
            },
        )

    assert response.status_code == 201
    assert model_service.calls == [
        (
            "create",
            {
                "name": "default",
                "model_type": "llm",
                "endpoint": "http://llm:8000/v1",
                "model_name": "mistral",
                "batch_size": 32,
                "timeout": 30.0,
                "extra": {},
                "is_default": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_model_endpoint_forwards_only_provided_fields(async_client_factory):
    """Model endpoint updates should exclude omitted fields."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/model-endpoints/llm/default",
            json={"endpoint": "http://llm-next:8000/v1", "timeout": 60},
        )

    assert response.status_code == 200
    assert model_service.calls == [
        (
            "update",
            {
                "name": "default",
                "model_type": "llm",
                "endpoint": "http://llm-next:8000/v1",
                "timeout": 60.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_model_endpoint_maps_name_to_new_name(async_client_factory):
    """Model endpoint rename should use the service rename field."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/model-endpoints/llm/default",
            json={"name": "mistral-small"},
        )

    assert response.status_code == 200
    assert model_service.calls == [
        (
            "update",
            {
                "name": "default",
                "model_type": "llm",
                "new_name": "mistral-small",
            },
        )
    ]


@pytest.mark.asyncio
async def test_model_endpoint_read_responses_hide_stored_api_key(async_client_factory):
    model_service = FakeModelEndpointService()
    model_service.endpoint_extra = {
        "implementation": "vllm",
        "api_key": "secret-token",
        "temperature": 0.2,
    }
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.get("/model-endpoints/llm/default")

    assert response.status_code == 200
    payload = response.json()
    assert "secret-token" not in response.text
    assert payload["has_api_key"] is True
    assert payload["extra"] == {"api_key": "sec********", "implementation": "vllm", "temperature": 0.2}


@pytest.mark.asyncio
async def test_model_endpoint_reveal_api_key_returns_stored_secret(async_client_factory, monkeypatch):
    model_service = FakeModelEndpointService()
    model_service.endpoint_extra = {"api_key": "secret-token", "implementation": "vllm"}
    app = _build_app(model_service=model_service)
    logs: list[tuple[dict[str, Any], str]] = []

    class FakeLogger:
        def __init__(self, context: dict[str, Any] | None = None) -> None:
            self.context = context or {}

        def bind(self, **kwargs: Any) -> FakeLogger:
            return FakeLogger({**self.context, **kwargs})

        def info(self, message: str) -> None:
            logs.append((self.context, message))

    monkeypatch.setattr(model_endpoints, "logger", FakeLogger())

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/reveal-api-key")

    assert response.status_code == 200
    assert response.json() == {"api_key": "secret-token"}
    assert logs == [
        (
            {"model_type": "llm", "name": "default", "has_api_key": True},
            "Model endpoint API key revealed.",
        )
    ]


@pytest.mark.asyncio
async def test_validate_model_endpoint_uses_route_identity(async_client_factory):
    """Endpoint validation should resolve route identity before probing."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/validate")

    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert model_service.calls == [
        ("get", {"name": "default", "model_type": "llm"}),
        ("validate", {"url": "http://llm:8000/v1", "model_type": "llm", "model_name": "mistral", "api_key": None}),
    ]


@pytest.mark.asyncio
async def test_validate_model_endpoint_uses_stored_api_key(async_client_factory):
    """Endpoint validation should authenticate with the stored endpoint key."""
    model_service = FakeModelEndpointService()
    model_service.endpoint_extra = {"api_key": "secret-token"}
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/validate")

    assert response.status_code == 200
    assert model_service.calls == [
        ("get", {"name": "default", "model_type": "llm"}),
        (
            "validate",
            {"url": "http://llm:8000/v1", "model_type": "llm", "model_name": "mistral", "api_key": "secret-token"},
        ),
    ]


@pytest.mark.asyncio
async def test_validate_endpoint_draft_forwards_body_without_lookup(async_client_factory):
    """The draft-validate route probes arbitrary *unsaved* values: it forwards the
    request body straight to the service with no prior endpoint lookup/persist."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/validate",
            json={
                "endpoint": "http://candidate:8000/v1",
                "model_type": "stt",
                "model_name": "mistral-small",
                "api_key": "draft-key",
            },
        )

    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert model_service.calls == [
        (
            "validate",
            {
                "url": "http://candidate:8000/v1",
                "model_type": "stt",
                "model_name": "mistral-small",
                "api_key": "draft-key",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_validate_endpoint_draft_can_reuse_stored_api_key(async_client_factory):
    model_service = FakeModelEndpointService()
    model_service.endpoint_extra = {"api_key": "secret-token"}
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/validate",
            json={
                "endpoint": "http://llm:8000/v1/",
                "model_name": "mistral-small",
                "stored_api_key_model_type": "llm",
                "stored_api_key_name": "default",
            },
        )

    assert response.status_code == 200
    assert model_service.calls == [
        ("get", {"name": "default", "model_type": "llm"}),
        (
            "validate",
            {"url": "http://llm:8000/v1", "model_type": None, "model_name": "mistral-small", "api_key": "secret-token"},
        ),
    ]


@pytest.mark.asyncio
async def test_validate_endpoint_draft_rejects_stored_key_for_different_endpoint(async_client_factory):
    model_service = FakeModelEndpointService()
    model_service.endpoint_extra = {"api_key": "secret-token"}
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/validate",
            json={
                "endpoint": "http://candidate:8000/v1",
                "model_name": "mistral-small",
                "stored_api_key_model_type": "llm",
                "stored_api_key_name": "default",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Stored API key can only be reused with its saved endpoint URL."
    assert model_service.calls == [
        ("get", {"name": "default", "model_type": "llm"}),
    ]


@pytest.mark.asyncio
async def test_validate_endpoint_draft_defaults_optional_fields(async_client_factory):
    """``model_name`` and ``api_key`` are optional in the draft body."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/validate",
            json={"endpoint": "http://candidate:8000/v1"},
        )

    assert response.status_code == 200
    assert model_service.calls == [
        ("validate", {"url": "http://candidate:8000/v1", "model_type": None, "model_name": None, "api_key": None}),
    ]


@pytest.mark.asyncio
async def test_set_default_model_endpoint_returns_promoted_endpoint(async_client_factory):
    """Default promotion should return the promoted endpoint row."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/set-default")

    assert response.status_code == 200
    assert response.json()["is_default"] is True
    assert model_service.calls == [
        ("set_default", {"name": "default", "model_type": "llm"}),
        ("get", {"name": "default", "model_type": "llm"}),
    ]


# --------------------------------------------------------------------------- #
# Auto-probed max_model_len cache refresh after an LLM endpoint write (#639) —
# config.models.llm itself is refreshed synchronously inside the service call
# (ModelEndpointService.load_all()), but the separate /v1/models auto-probe
# cache otherwise only refreshes once at process startup. These tests stub
# out prime_max_model_tokens itself (no real network calls) and just verify
# it's invoked — its own probing/caching behavior is covered by
# tests/unit/test_token_validation.py::TestPrimeMaxModelTokens.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_llm_endpoint_reprimes_token_cache(async_client_factory, monkeypatch):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    calls: list[str] = []

    async def fake_prime() -> None:
        calls.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={
                "name": "mistral",
                "model_type": "llm",
                "endpoint": "http://mistral:8000/v1",
                "model_name": "mistral",
            },
        )

    assert response.status_code == 201
    assert calls == ["primed"]


@pytest.mark.asyncio
async def test_create_non_llm_endpoint_does_not_reprime_token_cache(async_client_factory, monkeypatch):
    """The auto-probe cache is LLM-only — writes to embedder/reranker/vlm
    endpoints must not trigger a refresh."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    calls: list[str] = []

    async def fake_prime() -> None:
        calls.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={"name": "embedder-1", "model_type": "embedder", "endpoint": "http://emb:8000/v1"},
        )

    assert response.status_code == 201
    assert calls == []


@pytest.mark.asyncio
async def test_update_llm_endpoint_reprimes_token_cache(async_client_factory, monkeypatch):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    calls: list[str] = []

    async def fake_prime() -> None:
        calls.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)

    async with async_client_factory(app) as client:
        response = await client.put("/model-endpoints/llm/default", json={"timeout": 45})

    assert response.status_code == 200
    assert calls == ["primed"]


@pytest.mark.asyncio
async def test_delete_llm_endpoint_reprimes_token_cache(async_client_factory, monkeypatch):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    calls: list[str] = []

    async def fake_prime() -> None:
        calls.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)

    async with async_client_factory(app) as client:
        response = await client.delete("/model-endpoints/llm/default")

    assert response.status_code == 204
    assert calls == ["primed"]


@pytest.mark.asyncio
async def test_set_default_llm_endpoint_reprimes_token_cache(async_client_factory, monkeypatch):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    calls: list[str] = []

    async def fake_prime() -> None:
        calls.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/set-default")

    assert response.status_code == 200
    assert calls == ["primed"]


@pytest.mark.asyncio
async def test_reprime_failure_does_not_fail_the_request(async_client_factory, monkeypatch):
    """A cache-refresh failure is logged, not raised — the admin's write must
    still succeed even if the post-write auto-probe refresh fails."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async def failing_prime() -> None:
        raise RuntimeError("probe boom")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", failing_prime)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={
                "name": "mistral",
                "model_type": "llm",
                "endpoint": "http://mistral:8000/v1",
                "model_name": "mistral",
            },
        )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_preset_options_return_registered_choices(async_client_factory):
    """Preset options should expose available registry choices."""
    app = _build_app()

    async with async_client_factory(app) as client:
        response = await client.get("/presets/options")

    assert response.status_code == 200
    body = response.json()
    assert body["chunking_strategies"] == ["recursive_splitter", "structured_section"]
    assert set(body["retrieval_types"]) == {"single", "multiQuery", "hyde"}
    assert body["reranker_providers"] == ["infinity", "openai", "tei"]


@pytest.mark.asyncio
async def test_create_preset_forwards_schema_payload(async_client_factory):
    """Preset creation should pass normalized schema data to the service."""
    preset_service = FakePresetService()
    app = _build_app(preset_service=preset_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/presets/",
            json={"name": " default ", "preset_type": "retrieval", "config": {"type": "single"}},
        )

    assert response.status_code == 201
    assert preset_service.calls == [
        ("create", {"name": "default", "preset_type": "retrieval", "config": {"type": "single"}})
    ]


@pytest.mark.asyncio
async def test_update_preset_forwards_only_provided_fields(async_client_factory):
    """Preset updates should exclude omitted fields."""
    preset_service = FakePresetService()
    app = _build_app(preset_service=preset_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/presets/retrieval/default",
            json={"config": {"type": "hyde", "top_k": 20}},
        )

    assert response.status_code == 200
    assert preset_service.calls == [
        (
            "update",
            {
                "name": "default",
                "preset_type": "retrieval",
                "config": {"type": "hyde", "top_k": 20},
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_preset_maps_name_to_new_name(async_client_factory):
    """Preset rename should use the service rename field."""
    preset_service = FakePresetService()
    app = _build_app(preset_service=preset_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/presets/retrieval/default",
            json={"name": "legal"},
        )

    assert response.status_code == 200
    assert preset_service.calls == [
        (
            "update",
            {
                "name": "default",
                "preset_type": "retrieval",
                "new_name": "legal",
            },
        )
    ]


# --------------------------------------------------------------------------- #
# Stale-window closure: the service swaps config.models.llm synchronously, but
# the /v1/models probe runs in a background task. Between the two, the probed
# cache still describes the *previous* endpoint — so a chat request would be
# answered by the new endpoint and preflighted against the old one's limit.
# The route therefore invalidates inline (degrading to the conservative global
# fallback) and only re-probes afterwards.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_llm_write_invalidates_probed_cache_before_the_background_refresh(async_client_factory, monkeypatch):
    """Invalidation must happen inline, ahead of the background re-probe.

    Ordering is the whole point: if the invalidation ran inside the background
    task it would land no earlier than the re-probe it precedes, leaving the
    stale window exactly as wide as before.
    """
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    events: list[str] = []

    async def fake_prime() -> None:
        events.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)
    monkeypatch.setattr(model_endpoints, "invalidate_max_model_tokens", lambda: events.append("invalidated"))

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={
                "name": "mistral",
                "model_type": "llm",
                "endpoint": "http://mistral:8000/v1",
                "model_name": "mistral",
            },
        )

    assert response.status_code == 201
    assert events == ["invalidated", "primed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("put", "/model-endpoints/llm/default", {"timeout": 45}),
        ("delete", "/model-endpoints/llm/default", None),
        ("post", "/model-endpoints/llm/default/set-default", None),
    ],
)
async def test_every_llm_mutation_invalidates_the_probed_cache(
    async_client_factory, monkeypatch, method, path, payload
):
    """Update, delete and set-default repoint the registry just like create."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    events: list[str] = []

    async def fake_prime() -> None:
        events.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)
    monkeypatch.setattr(model_endpoints, "invalidate_max_model_tokens", lambda: events.append("invalidated"))

    async with async_client_factory(app) as client:
        kwargs = {"json": payload} if payload is not None else {}
        response = await getattr(client, method)(path, **kwargs)

    assert response.status_code in (200, 204)
    assert events == ["invalidated", "primed"]


@pytest.mark.asyncio
async def test_non_llm_write_does_not_invalidate_the_probed_cache(async_client_factory, monkeypatch):
    """The probed cache holds LLM entries only — a non-LLM write must not clear
    every other endpoint's probed limit as a side effect."""
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    events: list[str] = []

    async def fake_prime() -> None:
        events.append("primed")

    monkeypatch.setattr(model_endpoints, "prime_max_model_tokens", fake_prime)
    monkeypatch.setattr(model_endpoints, "invalidate_max_model_tokens", lambda: events.append("invalidated"))

    async with async_client_factory(app) as client:
        response = await client.put("/model-endpoints/embedder/default", json={"timeout": 45})

    assert response.status_code == 200
    assert events == []


# --------------------------------------------------------------------------- #
# The LLM token-budget keys are validated for LLM endpoints only. They are
# meaningless elsewhere, so enforcing their shape globally would reserve the
# names for every endpoint type — and since the admin UI preserves same-named
# non-LLM keys verbatim, an untouched endpoint would 422 on its next save.
# UpdateModelEndpointRequest has no model_type, so the route scopes the check
# from the path parameter.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["max_llm_context_size", "max_output_tokens"])
async def test_update_llm_endpoint_rejects_bad_token_budget(async_client_factory, key):
    """An LLM update still gets the positive-int rule, now applied route-side."""
    from api.error_handlers import register_error_handlers

    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    register_error_handlers(app)

    async with async_client_factory(app) as client:
        response = await client.put("/model-endpoints/llm/default", json={"extra": {key: 0}})

    assert response.status_code == 422
    assert key in response.text
    assert model_service.calls == []  # rejected before reaching the service


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["max_llm_context_size", "max_output_tokens"])
@pytest.mark.parametrize("model_type", ["embedder", "reranker", "vlm"])
async def test_update_non_llm_endpoint_accepts_same_named_extra_keys(async_client_factory, key, model_type):
    """A non-LLM endpoint may carry these names as provider metadata of any
    shape — re-saving it must not 422."""
    from api.error_handlers import register_error_handlers

    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    register_error_handlers(app)

    async with async_client_factory(app) as client:
        response = await client.put(f"/model-endpoints/{model_type}/default", json={"extra": {key: "auto"}})

    assert response.status_code == 200
    assert model_service.calls[0][1]["extra"] == {key: "auto"}


@pytest.mark.asyncio
async def test_update_stt_endpoint_validates_model_and_language_hint(async_client_factory):
    """The route knows the endpoint type that the generic update schema lacks."""
    from api.error_handlers import register_error_handlers

    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)
    register_error_handlers(app)

    async with async_client_factory(app) as client:
        invalid = await client.put("/model-endpoints/stt/default", json={"model_name": ""})
        valid = await client.put("/model-endpoints/stt/default", json={"extra": {"language": "fr"}})

    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert model_service.calls == [("update", {"name": "default", "model_type": "stt", "extra": {"language": "fr"}})]
