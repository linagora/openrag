"""Unit tests for ModelEndpointService (Phase 14E)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _row_payload(**kwargs):
    """Build a model endpoint row payload with targeted overrides."""
    payload = {
        "name": "default",
        "model_type": "embedder",
        "endpoint": "http://vllm:8000/v1",
        "model_name": "jina-v3",
        "batch_size": 32,
        "timeout": 30.0,
        "extra": {},
        "is_default": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    payload.update(kwargs)
    return payload


def _make_row(**kwargs):
    from core.config.model_endpoints import ModelEndpointRow

    return ModelEndpointRow(**_row_payload(**kwargs))


def _make_unvalidated_row(**kwargs):
    from core.config.model_endpoints import ModelEndpointRow

    return ModelEndpointRow.model_construct(**_row_payload(**kwargs))


class _FakeEndpointRepo:
    def __init__(self, rows: list | None = None):
        from core.config.model_endpoints import ModelEndpointRow

        self._store: dict[tuple[str, str], ModelEndpointRow] = {}
        self.calls: list[tuple[str, tuple]] = []
        for r in rows or []:
            self._store[(r.name, r.model_type)] = r

    async def create(self, row):
        self._store[(row.name, row.model_type)] = row
        self.calls.append(("create", (row.name, row.model_type)))
        return row

    async def get(self, name: str, model_type: str):
        self.calls.append(("get", (name, model_type)))
        return self._store.get((name, model_type))

    async def list_all(self, model_type: str | None = None):
        self.calls.append(("list_all", (model_type,)))
        rows = list(self._store.values())
        if model_type is not None:
            rows = [r for r in rows if r.model_type == model_type]
        return rows

    async def update(self, name: str, model_type: str, **fields):
        row = self._store.get((name, model_type))
        if row is None:
            return None
        updated = row.model_copy(update=fields)
        self._store[(name, model_type)] = updated
        return updated

    async def rename(self, name: str, model_type: str, new_name: str) -> None:
        row = self._store.pop((name, model_type), None)
        if row:
            self._store[(new_name, model_type)] = row.model_copy(update={"name": new_name})

    async def delete(self, name: str, model_type: str) -> bool:
        return self._store.pop((name, model_type), None) is not None

    async def set_default(self, model_type: str, name: str) -> None:
        for key, row in list(self._store.items()):
            self._store[key] = row.model_copy(update={"is_default": key[0] == name and key[1] == model_type})


def _make_service(repo=None, rows=None, settings=None):
    from core.config.root import Settings
    from services.orchestrators.model_endpoint_service import ModelEndpointService

    return ModelEndpointService(
        model_endpoint_repo=repo or _FakeEndpointRepo(rows),
        config=settings or Settings(),
    )


# ------------------------------------------------------------------
# seed_defaults
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_defaults_inserts_embedder_when_empty():
    repo = _FakeEndpointRepo()
    svc = _make_service(repo)
    await svc.seed_defaults()

    creates = [c for c in repo.calls if c[0] == "create"]
    assert any(name_type[1] == "embedder" for _, name_type in creates)


@pytest.mark.asyncio
async def test_seed_defaults_skips_type_when_rows_exist():
    existing = _make_row(name="custom", model_type="embedder", is_default=True)
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.seed_defaults()

    creates = [c for c in repo.calls if c[0] == "create"]
    assert not any(name_type[1] == "embedder" for _, name_type in creates)


@pytest.mark.asyncio
async def test_seed_defaults_skips_empty_endpoint(monkeypatch):
    # Settings().llm.base_url defaults to "" and Settings().llm.model to "".
    # As long as no LLM_ENDPOINT env var is set, seed_defaults should skip llm.
    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    repo = _FakeEndpointRepo()
    svc = _make_service(repo)
    await svc.seed_defaults()

    creates = [c for c in repo.calls if c[0] == "create"]
    assert not any(name_type[1] == "llm" for _, name_type in creates)


@pytest.mark.asyncio
async def test_seed_defaults_seeds_disabled_reranker_when_configured(monkeypatch):
    from core.config.root import Settings

    monkeypatch.delenv("RERANKER_ENDPOINT", raising=False)
    # A disabled reranker is still catalogued as long as it is configured
    # (base_url set): registration is about availability, while activation is
    # the retrieval preset's enable_reranker kill-switch.
    settings = Settings(reranker={"provider": "infinity", "enabled": False})
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)
    await svc.seed_defaults()

    creates = [c for c in repo.calls if c[0] == "create"]
    assert any(name_type[1] == "reranker" for _, name_type in creates)


@pytest.mark.asyncio
async def test_seed_defaults_skips_reranker_when_unconfigured(monkeypatch):
    from core.config.root import Settings

    monkeypatch.delenv("RERANKER_ENDPOINT", raising=False)
    # No base_url configured → nothing to advertise, so the reranker is skipped.
    settings = Settings(reranker={"provider": "openai", "base_url": ""})
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)
    await svc.seed_defaults()

    creates = [c for c in repo.calls if c[0] == "create"]
    assert not any(name_type[1] == "reranker" for _, name_type in creates)


# ------------------------------------------------------------------
# load_all
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_all_populates_config_models():
    from core.config.root import Settings

    rows = [
        _make_row(name="jina", model_type="embedder", is_default=True),
        _make_row(name="mistral", model_type="llm", is_default=True),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    settings = Settings()
    svc = _make_service(repo, settings=settings)

    await svc.load_all()

    assert "jina" in settings.models.embedder
    assert "default" in settings.models.embedder
    assert "mistral" in settings.models.llm
    assert "default" in settings.models.llm


@pytest.mark.asyncio
async def test_load_all_no_default_alias_without_is_default():
    from core.config.root import Settings

    rows = [_make_row(name="jina", model_type="embedder", is_default=False)]
    repo = _FakeEndpointRepo(rows=rows)
    settings = Settings()
    svc = _make_service(repo, settings=settings)

    await svc.load_all()

    assert "jina" in settings.models.embedder
    assert "default" not in settings.models.embedder


# ------------------------------------------------------------------
# create_model_endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_model_endpoint_rejects_invalid_type():
    from core.utils.exceptions import ValidationError

    svc = _make_service()
    row = _make_unvalidated_row(model_type="unknown_type")
    with pytest.raises(ValidationError, match="Invalid model_type"):
        await svc.create_model_endpoint(row)


@pytest.mark.asyncio
async def test_create_model_endpoint_raises_409_on_duplicate():
    from core.utils.exceptions import ValidationError

    existing = _make_row()
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)

    with pytest.raises(ValidationError) as exc_info:
        await svc.create_model_endpoint(_make_row())
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_model_endpoint_inserts_and_returns_row():
    repo = _FakeEndpointRepo()
    svc = _make_service(repo)
    row = _make_row(name="new-endpoint")

    result = await svc.create_model_endpoint(row)

    assert result.name == "new-endpoint"
    assert any(c[0] == "create" for c in repo.calls)


# ------------------------------------------------------------------
# get_model_endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_model_endpoint_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.get_model_endpoint("ghost", "embedder")


@pytest.mark.asyncio
async def test_get_model_endpoint_returns_row_when_found():
    existing = _make_row(name="jina")
    svc = _make_service(rows=[existing])

    row = await svc.get_model_endpoint("jina", "embedder")
    assert row.name == "jina"


# ------------------------------------------------------------------
# update_model_endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_model_endpoint_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.update_model_endpoint("ghost", "embedder", endpoint="http://new:8000/v1")


@pytest.mark.asyncio
async def test_update_model_endpoint_renames_and_evicts_cache():
    existing = _make_row(name="old-name")
    repo = _FakeEndpointRepo(rows=[existing])
    cache: dict = {"old-name": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.update_model_endpoint("old-name", "embedder", new_name="new-name")

    assert "old-name" not in cache
    assert ("old-name", "embedder") not in repo._store
    assert ("new-name", "embedder") in repo._store


# ------------------------------------------------------------------
# delete_model_endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_model_endpoint_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.delete_model_endpoint("ghost", "embedder")


@pytest.mark.asyncio
async def test_delete_model_endpoint_raises_422_when_last():
    from core.utils.exceptions import ValidationError

    existing = _make_row()
    svc = _make_service(rows=[existing])
    with pytest.raises(ValidationError, match="last"):
        await svc.delete_model_endpoint("default", "embedder")


@pytest.mark.asyncio
async def test_delete_model_endpoint_removes_row_and_evicts_cache():
    rows = [
        _make_row(name="jina", is_default=True),
        _make_row(name="e5", is_default=False),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    cache: dict = {"jina": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.delete_model_endpoint("jina", "embedder")

    assert ("jina", "embedder") not in repo._store
    assert "jina" not in cache


# ------------------------------------------------------------------
# set_default
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_default_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.set_default("embedder", "ghost")


@pytest.mark.asyncio
async def test_set_default_calls_repo_and_reloads():
    rows = [
        _make_row(name="jina", is_default=True),
        _make_row(name="e5", is_default=False),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    cache: dict = {"default": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.set_default("embedder", "e5")

    assert "default" not in cache
    assert any(c[0] == "list_all" for c in repo.calls)


# ------------------------------------------------------------------
# validate_endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_endpoint_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()

    with pytest.raises(NotFoundError):
        await svc.validate_endpoint(name="ghost", model_type="llm")


@pytest.mark.asyncio
async def test_validate_endpoint_uses_registered_endpoint(monkeypatch):
    import httpx

    row = _make_row(
        name="mistral",
        model_type="llm",
        endpoint="http://llm:8000/v1",
        model_name="mistral-small",
    )
    svc = _make_service(rows=[row])
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "mistral-small"}]}

    class FakeClient:
        def __init__(self, *, timeout):
            assert timeout == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(name="mistral", model_type="llm")

    assert calls == ["http://llm:8000/v1/models"]
    assert result == {
        "reachable": True,
        "model_found": True,
        "models_served": ["mistral-small"],
        "detail": None,
    }
