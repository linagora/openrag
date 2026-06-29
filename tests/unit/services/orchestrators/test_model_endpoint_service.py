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

    async def delete_and_promote_default(self, name: str, model_type: str) -> tuple[str, str | None]:
        names = sorted(k[0] for k in self._store if k[1] == model_type)
        self.calls.append(("delete_and_promote_default", (name, model_type)))
        if name not in names:
            return ("not_found", None)
        if len(names) <= 1:
            return ("last", None)
        was_default = self._store[(name, model_type)].is_default
        self._store.pop((name, model_type), None)
        promoted = None
        if was_default:
            promoted = next(n for n in names if n != name)
            for key, row in list(self._store.items()):
                if key[1] == model_type:
                    self._store[key] = row.model_copy(update={"is_default": key[0] == promoted})
        return ("ok", promoted)


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
async def test_seed_defaults_preserves_endpoint_api_keys(monkeypatch):
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings(
        embedder={"api_key": "embed-key"},
        llm={"base_url": "http://llm:8000/v1", "model": "mistral", "api_key": "llm-key"},
        vlm={"base_url": "http://vlm:8000/v1", "model": "pixtral", "api_key": "vlm-key"},
        reranker={"provider": "infinity", "api_key": "rerank-key"},
    )
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    rows = repo._store.values()
    assert {row.model_type: row.extra.get("api_key") for row in rows} == {
        "embedder": "embed-key",
        "llm": "llm-key",
        "vlm": "vlm-key",
        "reranker": "rerank-key",
    }


@pytest.mark.asyncio
async def test_seed_defaults_preserves_endpoint_timeouts_and_batch_size(monkeypatch):
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings(
        embedder={
            "base_url": "http://embedder:8000/v1",
            "model_name": "embed-model",
            "batch_size": 64,
            "timeout": 180,
        },
        llm={"base_url": "http://llm:8000/v1", "model": "mistral", "timeout": 45},
        vlm={"base_url": "http://vlm:8000/v1", "model": "pixtral", "timeout": 75},
        reranker={"provider": "infinity", "timeout": 25},
    )
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    rows = {row.model_type: row for row in repo._store.values()}
    assert rows["embedder"].batch_size == 64
    assert rows["embedder"].timeout == 180
    assert rows["llm"].timeout == 45
    assert rows["vlm"].timeout == 75
    assert rows["reranker"].timeout == 25


@pytest.mark.asyncio
async def test_seed_defaults_preserves_llm_and_vlm_enable_thinking(monkeypatch):
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings(
        llm={
            "base_url": "http://llm:8000/v1",
            "model": "qwen",
            "enable_thinking": False,
        },
        vlm={
            "base_url": "http://vlm:8000/v1",
            "model": "qwen-vl",
            "enable_thinking": True,
        },
    )
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    rows = {row.model_type: row for row in repo._store.values()}
    assert rows["llm"].extra["enable_thinking"] is False
    assert rows["vlm"].extra["enable_thinking"] is True


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


@pytest.mark.asyncio
async def test_update_default_endpoint_evicts_default_alias_cache():
    # Updating the current default must evict the 'default' cache key too, not just
    # the named one — else callers that resolved 'default' keep a stale client.
    existing = _make_row(name="jina", is_default=True)
    repo = _FakeEndpointRepo(rows=[existing])
    cache: dict = {"jina": object(), "default": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.update_model_endpoint("jina", "embedder", endpoint="http://new:8000/v1")

    assert "jina" not in cache
    assert "default" not in cache


@pytest.mark.asyncio
async def test_update_non_default_endpoint_keeps_default_alias_cache():
    # A non-default endpoint update must NOT evict the unrelated 'default' client.
    rows = [_make_row(name="e5", is_default=False), _make_row(name="jina", is_default=True)]
    repo = _FakeEndpointRepo(rows=rows)
    sentinel = object()
    cache: dict = {"e5": object(), "default": sentinel}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.update_model_endpoint("e5", "embedder", endpoint="http://new:8000/v1")

    assert "e5" not in cache
    assert cache.get("default") is sentinel


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


@pytest.mark.asyncio
async def test_delete_default_endpoint_promotes_survivor_and_evicts_default_cache():
    # Deleting the default must promote a surviving endpoint so the 'default' alias
    # keeps resolving (load_all only adds it for an is_default row), and evict the
    # now-stale 'default' client.
    rows = [
        _make_row(name="jina", is_default=True),
        _make_row(name="e5", is_default=False),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    cache: dict = {"jina": object(), "default": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.delete_model_endpoint("jina", "embedder")

    assert ("jina", "embedder") not in repo._store
    assert "default" not in cache
    # e5 is the lone survivor -> promoted so 'default' still resolves.
    assert repo._store[("e5", "embedder")].is_default is True


@pytest.mark.asyncio
async def test_delete_non_default_endpoint_leaves_default_untouched():
    rows = [
        _make_row(name="jina", is_default=True),
        _make_row(name="e5", is_default=False),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    svc = _make_service(repo)

    await svc.delete_model_endpoint("e5", "embedder")

    # Deleting a non-default endpoint leaves the existing default in place.
    assert repo._store[("jina", "embedder")].is_default is True


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
async def test_validate_endpoint_probes_url_and_model_name(monkeypatch):
    import httpx

    svc = _make_service()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "mistral-small"}]}

    class FakeClient:
        def __init__(self, *, timeout, headers):
            assert timeout == 5.0
            assert headers == {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint("http://llm:8000/v1", "mistral-small")

    assert calls == ["http://llm:8000/v1/models"]
    assert result == {
        "reachable": True,
        "model_found": True,
        "models_served": ["mistral-small"],
        "detail": None,
    }


@pytest.mark.asyncio
async def test_validate_endpoint_sends_api_key(monkeypatch):
    import httpx

    svc = _make_service()
    captured_headers: list[dict[str, str]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "mistral-small"}]}

    class FakeClient:
        def __init__(self, *, timeout, headers):
            captured_headers.append(headers)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    await svc.validate_endpoint("http://llm:8000/v1", "mistral-small", api_key="secret-token")

    assert captured_headers == [{"Authorization": "Bearer secret-token"}]
