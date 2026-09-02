"""Unit tests for ModelEndpointService (Phase 14E)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        self.calls.append(("update", (name, model_type)))
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


def _make_service(
    repo=None,
    rows=None,
    settings=None,
    partition_service=None,
    preset_service=None,
    prompt_service=None,
):
    from core.config.root import Settings
    from services.orchestrators.model_endpoint_service import ModelEndpointService

    return ModelEndpointService(
        model_endpoint_repo=repo or _FakeEndpointRepo(rows),
        config=settings or Settings(),
        partition_service=partition_service,
        preset_service=preset_service,
        prompt_service=prompt_service,
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
async def test_seed_defaults_ignores_concurrent_create_conflict():
    """A replica that loses the initial seed race must still finish booting."""
    from core.utils.exceptions import ValidationError

    attempted: list[str] = []

    class RacingRepo(_FakeEndpointRepo):
        async def create(self, row):
            attempted.append(row.model_type)
            if row.model_type == "stt":
                raise ValidationError(
                    "already exists",
                    status_code=409,
                    code="ENDPOINT_EXISTS",
                )
            return await super().create(row)

    repo = RacingRepo()
    await _make_service(repo).seed_defaults()

    assert "stt" in attempted


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
        loader={
            "transcriber": {
                "base_url": "http://stt:8000/v1",
                "model_name": "moss-transcribe-diarize",
                "api_key": "stt-key",
            }
        },
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
        "stt": "stt-key",
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
        loader={
            "transcriber": {
                "base_url": "http://stt:8000/v1",
                "model_name": "moss-transcribe-diarize",
                "timeout": 900,
                "max_concurrent_chunks": 3,
            }
        },
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
    assert rows["stt"].timeout == 900
    assert rows["stt"].batch_size == 3


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
async def test_seed_defaults_carries_llm_and_vlm_sampling_params(monkeypatch):
    """temperature/max_retries/logprobs must reach the seeded ``extra`` so a
    named LLM/VLM endpoint (built by di/factories.py splatting ``extra``) does
    not silently fall back to the provider's default sampling params (#720).
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings(
        llm={
            "base_url": "http://llm:8000/v1",
            "model": "qwen",
            "temperature": 0.3,
            "max_retries": 5,
            "logprobs": True,
        },
        vlm={
            "base_url": "http://vlm:8000/v1",
            "model": "qwen-vl",
            "temperature": 0.7,
            "max_retries": 1,
            "logprobs": False,
        },
    )
    repo = _FakeEndpointRepo()
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    rows = {row.model_type: row for row in repo._store.values()}
    assert rows["llm"].extra["temperature"] == 0.3
    assert rows["llm"].extra["max_retries"] == 5
    assert rows["llm"].extra["logprobs"] is True
    assert rows["vlm"].extra["temperature"] == 0.7
    assert rows["vlm"].extra["max_retries"] == 1
    assert rows["vlm"].extra["logprobs"] is False


@pytest.mark.asyncio
async def test_seed_defaults_backfills_missing_sampling_params_on_existing_llm_row(monkeypatch):
    """Regression test for the upgrade path (#720 review): endpoints created
    before the sampling-params fix never had temperature/max_retries/logprobs
    written to ``extra``. seed_defaults() must backfill the existing row
    instead of skipping the type outright, or upgraded deployments keep
    silently running at the provider default forever.
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    pre_fix_row = _make_row(
        name="default",
        model_type="llm",
        is_default=True,
        extra={"implementation": "vllm", "api_key": "llm-key"},
    )
    settings = Settings(
        llm={
            "base_url": "http://llm:8000/v1",
            "model": "mistral",
            "temperature": 0.3,
            "max_retries": 5,
            "logprobs": True,
        },
    )
    repo = _FakeEndpointRepo(rows=[pre_fix_row])
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    row = repo._store[("default", "llm")]
    assert row.extra["temperature"] == 0.3
    assert row.extra["max_retries"] == 5
    assert row.extra["logprobs"] is True
    assert row.extra["api_key"] == "llm-key"
    assert row.extra["implementation"] == "vllm"
    # No duplicate row was created — the existing one was updated in place.
    creates = [c for c in repo.calls if c[0] == "create"]
    assert not any(name_type[1] == "llm" for _, name_type in creates)


@pytest.mark.asyncio
async def test_seed_defaults_preserves_explicit_sampling_param_while_backfilling_others(monkeypatch):
    """Backfilling missing keys must not clobber a value already present on the
    row, even when other sampling keys on that same row are still missing.
    """
    from core.config.root import Settings

    monkeypatch.delenv("VLM_ENDPOINT", raising=False)
    monkeypatch.delenv("VLM_MODEL", raising=False)

    partially_fixed_row = _make_row(
        name="default",
        model_type="vlm",
        is_default=True,
        extra={"implementation": "vllm", "temperature": 0.9},
    )
    settings = Settings(
        vlm={
            "base_url": "http://vlm:8000/v1",
            "model": "pixtral",
            "temperature": 0.7,
            "max_retries": 1,
            "logprobs": False,
        },
    )
    repo = _FakeEndpointRepo(rows=[partially_fixed_row])
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    row = repo._store[("default", "vlm")]
    assert row.extra["temperature"] == 0.9  # explicit value left untouched
    assert row.extra["max_retries"] == 1  # missing key filled in
    assert row.extra["logprobs"] is False  # missing key filled in


@pytest.mark.asyncio
async def test_seed_defaults_backfill_does_not_clobber_fully_configured_row(monkeypatch):
    """A row that already has all three sampling keys must be left exactly as
    stored, even when current Settings would compute a different value.
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    already_fixed_row = _make_row(
        name="default",
        model_type="llm",
        is_default=True,
        extra={"implementation": "vllm", "temperature": 0.5, "max_retries": 3, "logprobs": True},
    )
    settings = Settings(llm={"base_url": "http://llm:8000/v1", "model": "mistral", "temperature": 0.9})
    repo = _FakeEndpointRepo(rows=[already_fixed_row])
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    row = repo._store[("default", "llm")]
    assert row.extra["temperature"] == 0.5
    assert row.extra["max_retries"] == 3
    assert row.extra["logprobs"] is True


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


@pytest.mark.asyncio
async def test_seed_defaults_leaves_existing_env_named_row_untouched_by_default(monkeypatch):
    """sync_on_boot defaults to False: an already-seeded row (name matches the
    env-derived slug) must survive an env-var change across restarts — the DB
    stays the source of truth until an admin (or an opt-in sync) changes it.
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="embed-model", model_type="embedder", model_name="embed-model", batch_size=512, is_default=True
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(embedder={"base_url": "http://embedder:8000/v1", "model_name": "embed-model", "batch_size": 64})
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    assert not any(c[0] == "update" for c in repo.calls)
    assert repo._store[("embed-model", "embedder")].batch_size == 512


@pytest.mark.asyncio
async def test_seed_defaults_syncs_env_named_row_when_sync_on_boot_enabled(monkeypatch):
    """sync_on_boot=True: the endpoint whose name matches the current
    env-derived slug is refreshed from Settings/env on every boot, so a Helm
    values change + pod rollout is enough — no admin API call needed.
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    # _sync_env_managed reads these directly, so an inherited value from the dev
    # or CI environment would make the "batch_size is preserved" assertion below
    # pass or fail for reasons that have nothing to do with the code under test.
    monkeypatch.delenv("EMBEDDER_BATCH_SIZE", raising=False)
    monkeypatch.delenv("EMBEDDER_TIMEOUT", raising=False)

    existing = _make_row(
        name="embed-model",
        model_type="embedder",
        model_name="embed-model",
        endpoint="http://old-embedder:8000/v1",
        batch_size=512,
        # Marked env-managed: a row whose endpoint has drifted from env is only
        # synced when it carries the marker — adoption by slug alone would not
        # touch it (see test_..._does_not_adopt_a_modified_row).
        extra={"api_key": "hand-set-secret", "managed_by": "env"},
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "embed-model", "batch_size": 64},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    updated = repo._store[("embed-model", "embedder")]
    # EMBEDDER_BATCH_SIZE is not set, so env does not own batch_size here and the
    # admin's 512 stands; endpoint/model_name are always env-owned and do sync.
    assert updated.batch_size == 512
    assert updated.endpoint == "http://embedder:8000/v1"
    # Sync now writes `extra` (it must, to rotate a key), but a hand-set key still
    # survives: env has no real key here, only the `EMPTY` placeholder, which is
    # never treated as a credential. The row is stamped as env-managed so a later
    # model change can find it by marker instead of by slug.
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE

    assert updated.extra == {"api_key": "hand-set-secret", ENV_MANAGED_KEY: ENV_MANAGED_VALUE}


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_never_touches_differently_named_endpoints(monkeypatch):
    """sync_on_boot=True must not clobber a hand-created endpoint that doesn't
    share the env-derived name, and must not create a competing default either.
    """
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(name="custom", model_type="embedder", batch_size=999, is_default=True)
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "embed-model", "batch_size": 64},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    embedder_calls = [c for c in repo.calls if c[0] in ("update", "create") and c[1][1] == "embedder"]
    assert not embedder_calls
    assert repo._store[("custom", "embedder")].batch_size == 999


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_follows_a_changed_model_slug(monkeypatch):
    """Changing the model must actually take effect under sync_on_boot.

    The row is found by its env-managed marker, not by the slug it was named
    after, so a new model is written onto the same row and the row is renamed
    to match. Before the marker this silently did nothing: the new slug matched
    no row, the seed declined to create a competing default, and the old model
    stayed live.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="old-model",
        model_type="embedder",
        model_name="old-model",
        batch_size=512,
        extra={"implementation": "vllm", ENV_MANAGED_KEY: ENV_MANAGED_VALUE},
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "new-model", "batch_size": 64},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    # The row keeps its name — partitions and presets store endpoint names by
    # value and nothing cascades a rename, so renaming would strand them.
    assert ("new-model", "embedder") not in repo._store
    synced = repo._store[("old-model", "embedder")]
    assert synced.model_name == "new-model"
    assert synced.endpoint == "http://embedder:8000/v1"


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_rotates_the_api_key(monkeypatch):
    """A rotated *_API_KEY must reach the row it owns.

    ``extra`` was previously never written by sync, so rotating the key left the
    old one in the DB and requests failed as soon as the provider revoked it.
    Only the key is taken from env — an admin's other ``extra`` keys survive.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="embed-model",
        model_type="embedder",
        model_name="embed-model",
        extra={
            "implementation": "vllm",
            "api_key": "old-revoked-key",
            "custom_kwarg": "set-by-admin",
            ENV_MANAGED_KEY: ENV_MANAGED_VALUE,
        },
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={
            "base_url": "http://embedder:8000/v1",
            "model_name": "embed-model",
            "api_key": "new-rotated-key",
        },
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    extra = repo._store[("embed-model", "embedder")].extra
    assert extra["api_key"] == "new-rotated-key"
    assert extra["custom_kwarg"] == "set-by-admin"


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_adopts_a_row_seeded_before_the_marker(monkeypatch):
    """Deployments upgraded from a build without the marker must not be stranded.

    The row has no marker, so it is matched by slug once and stamped — from then
    on it is found by marker and a model change can follow it.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="embed-model",
        model_type="embedder",
        model_name="embed-model",
        endpoint="http://embedder:8000/v1",  # still exactly what the seeder wrote
        extra={"implementation": "vllm"},
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "embed-model"},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    assert repo._store[("embed-model", "embedder")].extra[ENV_MANAGED_KEY] == ENV_MANAGED_VALUE


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_does_not_adopt_a_modified_row(monkeypatch):
    """A hand-created row that merely shares the slug must not be taken over.

    Before the marker existed the slug was the only handle on a seeded row, but a
    slug match alone cannot distinguish an old seed from an endpoint an admin
    named after the model. Adoption therefore requires the row to still match env
    exactly; this one has been re-pointed by hand, so sync leaves it alone.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="embed-model",
        model_type="embedder",
        model_name="embed-model",
        endpoint="http://admin-chosen-host:9000/v1",  # deliberately not the env value
        batch_size=512,
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "embed-model"},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    untouched = repo._store[("embed-model", "embedder")]
    assert untouched.endpoint == "http://admin-chosen-host:9000/v1"
    assert untouched.batch_size == 512
    assert ENV_MANAGED_KEY not in untouched.extra


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_keeps_admin_timeout_when_no_env_var_set(monkeypatch):
    """`llm` has no timeout env var at all, so sync must never write timeout.

    The seed always carries `s.llm.timeout`, so trusting the seed's presence
    overwrote an admin's 99 with the config default.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="chat-model",
        model_type="llm",
        model_name="chat-model",
        endpoint="http://llm:8000/v1",
        timeout=99.0,
        extra={ENV_MANAGED_KEY: ENV_MANAGED_VALUE},
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        llm={"base_url": "http://llm:8000/v1", "model": "chat-model"},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    assert repo._store[("chat-model", "llm")].timeout == 99.0


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_keeps_admin_batch_size_for_non_embedder_types(monkeypatch):
    """Sync must not invent values the environment never supplied.

    `_build_default_seeds` only carries a `batch_size` for `embedder`; the
    llm/vlm/reranker seeds have none. Defaulting to a literal therefore reset an
    admin-tuned row to 32 on every boot, even though nothing in env asked for a
    batch_size at all. The fallback is the row's own value.
    """
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    existing = _make_row(
        name="chat-model",
        model_type="llm",
        model_name="chat-model",
        batch_size=256,
        timeout=99.0,
        extra={ENV_MANAGED_KEY: ENV_MANAGED_VALUE},
        is_default=True,
    )
    repo = _FakeEndpointRepo(rows=[existing])
    settings = Settings(
        llm={"base_url": "http://llm:8000/v1", "model": "chat-model"},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    synced = repo._store[("chat-model", "llm")]
    assert synced.batch_size == 256, "env supplied no batch_size for llm — the admin value must stand"
    assert synced.endpoint == "http://llm:8000/v1"


@pytest.mark.asyncio
async def test_seed_defaults_sync_on_boot_will_not_rename_over_a_hand_created_name(monkeypatch):
    """The rename must never collide with an endpoint an admin created."""
    from core.config.model_endpoints import ENV_MANAGED_KEY, ENV_MANAGED_VALUE
    from core.config.root import Settings

    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    managed = _make_row(
        name="old-model",
        model_type="embedder",
        model_name="old-model",
        extra={ENV_MANAGED_KEY: ENV_MANAGED_VALUE},
        is_default=True,
    )
    hand_made = _make_row(name="new-model", model_type="embedder", model_name="something-else", batch_size=999)
    repo = _FakeEndpointRepo(rows=[managed, hand_made])
    settings = Settings(
        embedder={"base_url": "http://embedder:8000/v1", "model_name": "new-model"},
        models={"sync_on_boot": True},
    )
    svc = _make_service(repo, settings=settings)

    await svc.seed_defaults()

    # The hand-created row is intact and the managed row kept its old name.
    assert repo._store[("new-model", "embedder")].batch_size == 999
    assert repo._store[("new-model", "embedder")].model_name == "something-else"
    assert repo._store[("old-model", "embedder")].model_name == "new-model"


# ------------------------------------------------------------------
# load_all
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_all_populates_config_models():
    from core.config.root import Settings

    rows = [
        _make_row(name="jina", model_type="embedder", is_default=True),
        _make_row(name="mistral", model_type="llm", is_default=True),
        _make_row(name="moss", model_type="stt", model_name="moss-transcribe-diarize", is_default=True),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    settings = Settings()
    svc = _make_service(repo, settings=settings)

    await svc.load_all()

    assert "jina" in settings.models.embedder
    assert "default" in settings.models.embedder
    assert "mistral" in settings.models.llm
    assert "default" in settings.models.llm
    assert "moss" in settings.models.stt
    assert "default" in settings.models.stt
    assert settings.models.stt["moss"].name == "moss"
    assert settings.models.stt["default"].name == "moss"


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


class _FakePresetServiceForReload:
    def __init__(self):
        self.load_all_calls = 0

    async def load_all(self):
        self.load_all_calls += 1


class _FakePartitionServiceForReload:
    def __init__(self):
        self.load_partitions_calls = 0

    async def load_partitions(self):
        self.load_partitions_calls += 1


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_reloads_presets_then_partitions():
    """A rename cascades DB-side references (#770) inside the repo's own rename
    transaction (see PgModelEndpointRepository.rename), but those writes are
    invisible until PresetService / PartitionService reload their in-memory
    caches — pin that both get refreshed on a rename."""
    existing = _make_row(name="old-name", model_type="llm")
    repo = _FakeEndpointRepo(rows=[existing])
    preset_service = _FakePresetServiceForReload()
    partition_service = _FakePartitionServiceForReload()
    svc = _make_service(repo, partition_service=partition_service, preset_service=preset_service)

    await svc.update_model_endpoint("old-name", "llm", new_name="new-name")

    assert preset_service.load_all_calls == 1
    assert partition_service.load_partitions_calls == 1


@pytest.mark.asyncio
async def test_update_model_endpoint_without_rename_skips_preset_and_partition_reload():
    """A plain field update (no rename) touches no cross-referenced name, so
    it must not pay for a presets/partitions reload it doesn't need."""
    existing = _make_row(name="jina")
    repo = _FakeEndpointRepo(rows=[existing])
    preset_service = _FakePresetServiceForReload()
    partition_service = _FakePartitionServiceForReload()
    svc = _make_service(repo, partition_service=partition_service, preset_service=preset_service)

    await svc.update_model_endpoint("jina", "embedder", endpoint="http://new:8000/v1")

    assert preset_service.load_all_calls == 0
    assert partition_service.load_partitions_calls == 0


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_aliases_new_name_before_reload_awaits():
    """A request racing the rename must resolve `new_name` even before the
    presets/partitions reload below completes — the DB cascade (#770) has
    already repointed partitions/presets at it by the time the rename
    `await` returns, so the registry can't lag behind until `load_all()`."""
    existing = _make_row(name="old-name", model_type="llm", endpoint="http://old:8000/v1")
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.load_all()

    seen_during_reload = {}

    class _SnoopingPresetService:
        async def load_all(self):
            seen_during_reload["new-name"] = svc._config.models.llm.get("new-name")
            seen_during_reload["old-name"] = svc._config.models.llm.get("old-name")

    svc._preset_service = _SnoopingPresetService()

    await svc.update_model_endpoint("old-name", "llm", new_name="new-name")

    assert seen_during_reload["new-name"].endpoint == "http://old:8000/v1"
    assert seen_during_reload["old-name"].endpoint == "http://old:8000/v1"


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_keeps_both_names_resolvable_after_failed_reload():
    """If PresetService.load_all() (or PartitionService.load_partitions())
    raises mid-rename, the DB rename has already committed — the registry
    must still resolve both the old and the new name afterward, instead of
    being stuck answering only to the pre-rename one until process restart."""
    existing = _make_row(name="old-name", model_type="llm", endpoint="http://old:8000/v1")
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.load_all()

    class _FailingPresetService:
        async def load_all(self):
            raise RuntimeError("db blip")

    svc._preset_service = _FailingPresetService()

    with pytest.raises(RuntimeError):
        await svc.update_model_endpoint("old-name", "llm", new_name="new-name")

    assert svc._config.models.llm.get("old-name").endpoint == "http://old:8000/v1"
    assert svc._config.models.llm.get("new-name").endpoint == "http://old:8000/v1"


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_with_field_change_aliases_the_new_values():
    """A rename combined with a field change (e.g. a new endpoint URL) must
    alias `new_name`/`old_name` to the row this call just wrote, not to
    whatever the in-memory bucket held before the update ran — otherwise a
    reload failure right after would leave the registry silently serving the
    stale pre-update config under the DB-authoritative new name forever."""
    existing = _make_row(name="old-name", model_type="llm", endpoint="http://old:8000/v1")
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.load_all()

    class _FailingPresetService:
        async def load_all(self):
            raise RuntimeError("db blip")

    svc._preset_service = _FailingPresetService()

    with pytest.raises(RuntimeError):
        await svc.update_model_endpoint("old-name", "llm", new_name="new-name", endpoint="http://new:8000/v1")

    assert svc._config.models.llm.get("new-name").endpoint == "http://new:8000/v1"
    assert svc._config.models.llm.get("old-name").endpoint == "http://new:8000/v1"


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_aliases_carry_the_new_registry_name():
    """The aliased config must be named for what the DB now calls the row.

    ``_alias_renamed_name`` is handed the pre-rename row, so naming the config
    from it left ``name`` at ``old_name`` under both aliases until the final
    ``load_all()``. ``ModelEndpointConfig.name`` is the stable registry identity
    — and for STT it keys OpenAIAudioClient's limiter/client caches — so a
    request landing in that window would key its caches under the retired name.
    """
    existing = _make_row(name="old-name", model_type="stt", endpoint="http://old:8000/v1")
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.load_all()

    class _FailingPresetService:
        async def load_all(self):
            raise RuntimeError("db blip")

    svc._preset_service = _FailingPresetService()

    with pytest.raises(RuntimeError):
        await svc.update_model_endpoint("old-name", "stt", new_name="new-name")

    assert svc._config.models.stt.get("new-name").name == "new-name"
    # The old name stays resolvable for in-flight references, but it is an alias
    # to the renamed row — not a claim that the row is still called that.
    assert svc._config.models.stt.get("old-name").name == "new-name"


@pytest.mark.asyncio
async def test_update_model_endpoint_rename_evicts_stale_client_cache_before_failed_reload():
    """A cached *client instance* under old_name predates this call and can't
    know about a field change baked into the same rename — the factory checks
    its cache before the config registry, so it must be evicted eagerly (not
    only in the post-reload cleanup a failing reload would skip), or a
    request through old_name keeps getting the stale pre-update client."""
    existing = _make_row(name="old-name", model_type="llm", endpoint="http://old:8000/v1")
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.load_all()
    stale_client = object()
    cache: dict = {"old-name": stale_client}
    svc._client_caches["llm"] = cache

    class _FailingPresetService:
        async def load_all(self):
            raise RuntimeError("db blip")

    svc._preset_service = _FailingPresetService()

    with pytest.raises(RuntimeError):
        await svc.update_model_endpoint("old-name", "llm", new_name="new-name", endpoint="http://new:8000/v1")

    assert "old-name" not in cache
    assert "new-name" not in cache


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


@pytest.mark.asyncio
async def test_update_is_default_promotes_atomically_single_default():
    # Promoting via update({"is_default": True}) must route through the clear-then-set
    # path: the previous default is demoted (never two is_default=true rows) and the
    # 'default' alias client is evicted. Without this, a bare UPDATE would add a
    # second default and the alias would resolve to whichever row sorts last by name.
    rows = [
        _make_row(name="jina", is_default=True),
        _make_row(name="e5", is_default=False),
    ]
    repo = _FakeEndpointRepo(rows=rows)
    cache: dict = {"e5": object(), "default": object()}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.update_model_endpoint("e5", "embedder", is_default=True)

    defaults = sorted(name for (name, mt), row in repo._store.items() if mt == "embedder" and row.is_default)
    assert defaults == ["e5"], f"expected exactly one default (e5), got {defaults}"
    assert repo._store[("jina", "embedder")].is_default is False
    assert "default" not in cache


@pytest.mark.asyncio
async def test_update_is_default_false_does_not_demote_or_touch_default_cache():
    # is_default=False is a no-op: it must not clear the existing default (which would
    # leave the type with none) and must not evict the unrelated 'default' client.
    rows = [_make_row(name="e5", is_default=False), _make_row(name="jina", is_default=True)]
    repo = _FakeEndpointRepo(rows=rows)
    sentinel = object()
    cache: dict = {"e5": object(), "default": sentinel}
    svc = _make_service(repo)
    svc._client_caches["embedder"] = cache

    await svc.update_model_endpoint("e5", "embedder", is_default=False)

    assert repo._store[("jina", "embedder")].is_default is True
    assert cache.get("default") is sentinel


@pytest.mark.asyncio
async def test_update_extra_without_api_key_preserves_existing_secret():
    existing = _make_row(
        name="jina",
        extra={"implementation": "vllm", "api_key": "stored-key", "temperature": 0.1},
    )
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.update_model_endpoint("jina", "embedder", extra={"implementation": "vllm", "temperature": 0.2})

    updated = repo._store[("jina", "embedder")]
    assert updated.extra == {"implementation": "vllm", "temperature": 0.2, "api_key": "stored-key"}


@pytest.mark.asyncio
async def test_update_extra_preserves_nested_redacted_secrets():
    existing = _make_row(
        name="jina",
        extra={
            "implementation": "vllm",
            "auth": {
                "token": "stored-token",
                "headers": [{"api_key": "nested-key"}],
            },
        },
    )
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.update_model_endpoint(
        "jina",
        "embedder",
        extra={
            "implementation": "vllm",
            "auth": {
                "token": "<redacted>",
                "headers": [{"api_key": "<redacted>"}],
            },
            "temperature": 0.2,
        },
    )

    updated = repo._store[("jina", "embedder")]
    assert updated.extra == {
        "implementation": "vllm",
        "auth": {
            "token": "stored-token",
            "headers": [{"api_key": "nested-key"}],
        },
        "temperature": 0.2,
    }


@pytest.mark.asyncio
async def test_update_extra_with_new_api_key_rotates_existing_secret():
    existing = _make_row(name="jina", extra={"implementation": "vllm", "api_key": "old-key"})
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.update_model_endpoint("jina", "embedder", extra={"implementation": "vllm", "api_key": "new-key"})

    updated = repo._store[("jina", "embedder")]
    assert updated.extra == {"implementation": "vllm", "api_key": "new-key"}


@pytest.mark.asyncio
async def test_update_extra_with_empty_api_key_clears_existing_secret():
    existing = _make_row(name="jina", extra={"implementation": "vllm", "api_key": "old-key"})
    repo = _FakeEndpointRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.update_model_endpoint("jina", "embedder", extra={"implementation": "vllm", "api_key": ""})

    updated = repo._store[("jina", "embedder")]
    assert updated.extra == {"implementation": "vllm"}


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
        def __init__(self, *, timeout, headers, follow_redirects):
            assert timeout == 5.0
            assert headers == {}
            assert follow_redirects is False

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
        "transcription_supported": None,
        "detail": None,
    }


@pytest.mark.asyncio
async def test_validate_stt_endpoint_probes_transcription_capability_with_redirects_enabled(monkeypatch):
    import httpx

    prompt_service = SimpleNamespace(resolve_prompt=AsyncMock(return_value="  Prefer OpenRAG terms.  "))
    svc = _make_service(prompt_service=prompt_service)
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self._data = data or {}

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *, timeout, headers, follow_redirects):
            assert timeout == 5.0
            assert headers == {}
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(("get", url))
            return FakeResponse(200, {"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, url, *, data, files, follow_redirects, timeout):
            calls.append(("post", url))
            assert data == {
                "model": "moss-transcribe-diarize",
                "prompt": "Prefer OpenRAG terms.",
                "language": "fr",
                "response_format": "json",
                "temperature": "0",
                "timestamp_granularities[]": ["segment", "word"],
                "provider[diarize]": "true",
            }
            assert files["file"][0] == "openrag-stt-validation.wav"
            assert files["file"][2] == "audio/wav"
            assert files["file"][1][:4] == b"RIFF"
            assert len(files["file"][1]) == 32_044
            assert follow_redirects is True
            assert timeout.connect == 5.0
            assert timeout.write == 5.0
            assert timeout.pool == 5.0
            assert timeout.read == 180.0
            return FakeResponse(200, {"text": ""})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
        timeout=180,
        extra={
            "language": "fr",
            "response_format": "json",
            "temperature": 0,
            "timestamp_granularities": ["segment", "word"],
            "provider": {"diarize": True},
            "api_key": "must-not-be-forwarded",
            "implementation": "must-not-be-forwarded",
            "prompt": "must-not-be-forwarded",
        },
    )

    assert calls == [
        ("get", "http://moss:8000/v1/models"),
        ("post", "http://moss:8000/v1/audio/transcriptions"),
    ]
    prompt_service.resolve_prompt.assert_awaited_once_with("asr_transcription")
    assert result == {
        "reachable": True,
        "model_found": True,
        "models_served": ["moss-transcribe-diarize"],
        "transcription_supported": True,
        "detail": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "audio_payload", "expected_supported"),
    [
        ({}, {}, False),
        ({"response_format": "text"}, None, True),
    ],
)
async def test_validate_stt_endpoint_checks_success_response_shape(
    monkeypatch,
    extra,
    audio_payload,
    expected_supported,
):
    import httpx

    svc = _make_service()

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if self._payload is None:
                raise ValueError("not JSON")
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, _url):
            return FakeResponse({"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, _url, **_kwargs):
            return FakeResponse(audio_payload)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
        extra=extra,
    )

    assert result["transcription_supported"] is expected_supported
    assert result["detail"] == (
        None if expected_supported else "Transcription endpoint returned an incompatible response."
    )


@pytest.mark.asyncio
async def test_validate_stt_endpoint_uses_one_normalized_model_name(monkeypatch):
    """Model discovery and the authenticated probe must agree on the model."""
    import httpx

    svc = _make_service()
    probed_models: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload=None):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, _url):
            return FakeResponse({"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, _url, *, data, **_kwargs):
            probed_models.append(data["model"])
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "  moss-transcribe-diarize  ",
        model_type="stt",
    )

    assert result["model_found"] is True
    assert probed_models == ["moss-transcribe-diarize"]


@pytest.mark.asyncio
async def test_validate_stt_endpoint_rejects_missing_transcription_route(monkeypatch):
    import httpx

    svc = _make_service()

    class FakeResponse:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self._data = data or {}

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *, timeout, headers, follow_redirects):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse(200, {"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, url, **_kwargs):
            return FakeResponse(404)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
    )

    assert result == {
        "reachable": True,
        "model_found": True,
        "models_served": ["moss-transcribe-diarize"],
        "transcription_supported": False,
        "detail": "Endpoint does not support OpenAI-compatible audio transcriptions.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 422])
async def test_validate_stt_endpoint_rejects_unsuccessful_audio_probe(monkeypatch, status_code):
    """A malformed or rejected real probe must not validate STT credentials."""
    import httpx

    svc = _make_service()

    class FakeResponse:
        def __init__(self, response_status, data=None):
            self.status_code = response_status
            self._data = data or {}

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse(200, {"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, url, **kwargs):
            assert kwargs["data"] == {"model": "moss-transcribe-diarize"}
            assert kwargs["files"]["file"][1][:4] == b"RIFF"
            return FakeResponse(status_code)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        api_key="bad-key",
        model_type="stt",
    )

    assert result == {
        "reachable": True,
        "model_found": True,
        "models_served": ["moss-transcribe-diarize"],
        "transcription_supported": False,
        "detail": f"Transcription validation request returned HTTP {status_code}.",
    }


@pytest.mark.asyncio
async def test_validate_stt_endpoint_skips_audio_probe_when_model_is_not_served(monkeypatch):
    """Do not spend STT capacity once the public model list rejects the model."""
    import httpx

    svc = _make_service()
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, response_status, data=None):
            self.status_code = response_status
            self._data = data or {}

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append("get")
            return FakeResponse(200, {"data": [{"id": "another-model"}]})

        async def post(self, url, **_kwargs):
            calls.append("post")
            return FakeResponse(200)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
    )

    assert calls == ["get"]
    assert result == {
        "reachable": True,
        "model_found": False,
        "models_served": ["another-model"],
        "transcription_supported": None,
        "detail": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("model_payload", [ValueError("invalid JSON"), {"data": "not-a-list"}])
async def test_validate_stt_endpoint_probes_audio_when_model_list_is_invalid(monkeypatch, model_payload):
    import httpx

    svc = _make_service()
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(("get", url))
            return FakeResponse(200, model_payload)

        async def post(self, url, **_kwargs):
            calls.append(("post", url))
            return FakeResponse(200, {"text": ""})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
    )

    assert calls == [
        ("get", "http://moss:8000/v1/models"),
        ("post", "http://moss:8000/v1/audio/transcriptions"),
    ]
    assert result == {
        "reachable": True,
        "model_found": None,
        "models_served": None,
        "transcription_supported": True,
        "detail": "Endpoint returned an invalid model list.",
    }


@pytest.mark.asyncio
async def test_validate_stt_endpoint_probes_audio_when_model_list_times_out(monkeypatch):
    import httpx

    svc = _make_service()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"text": ""}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, request_url):
            calls.append("get")
            raise httpx.ReadTimeout("model list cold start")

        async def post(self, request_url, **kwargs):
            calls.append("post")
            assert kwargs["timeout"].read == 900
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        model_type="stt",
        timeout=900,
    )

    assert calls == ["get", "post"]
    assert result == {
        "reachable": True,
        "model_found": None,
        "models_served": None,
        "transcription_supported": True,
        "detail": "Model list request timed out.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_validate_stt_endpoint_rejects_auth_failure_on_audio_probe(monkeypatch, status_code):
    import httpx

    svc = _make_service()

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse(200, {"data": [{"id": "moss-transcribe-diarize"}]})

        async def post(self, url, **_kwargs):
            return FakeResponse(status_code)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        api_key="bad-key",
        model_type="stt",
    )

    assert result["reachable"] is True
    assert result["model_found"] is True
    assert result["transcription_supported"] is False
    assert (
        result["detail"] == f"Transcription capability check was rejected with HTTP {status_code}. Check the API key."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_validate_stt_endpoint_stops_after_model_list_auth_failure(monkeypatch, status_code):
    import httpx

    svc = _make_service()
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, response_status):
            self.status_code = response_status

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(("get", url))
            return FakeResponse(status_code)

        async def post(self, url, **_kwargs):
            calls.append(("post", url))
            return FakeResponse(422)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://moss:8000/v1",
        "moss-transcribe-diarize",
        api_key="bad-key",
        model_type="stt",
    )

    assert calls == [("get", "http://moss:8000/v1/models")]
    assert result == {
        "reachable": False,
        "model_found": None,
        "models_served": None,
        "transcription_supported": False,
        "detail": f"Model list request was rejected with HTTP {status_code}. Check the API key.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_validate_non_stt_endpoint_keeps_auth_gated_model_list_reachable(monkeypatch, status_code):
    """An HTTP response proves reachability even when a non-STT model list is scoped."""
    import httpx

    svc = _make_service()

    class FakeResponse:
        def __init__(self, response_status):
            self.status_code = response_status

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse(status_code)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await svc.validate_endpoint(
        "http://llm:8000/v1",
        "mistral-small",
        api_key="scoped-key",
        model_type="llm",
    )

    assert result == {
        "reachable": True,
        "model_found": None,
        "models_served": None,
        "transcription_supported": None,
        "detail": f"Model list returned HTTP {status_code}.",
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
        def __init__(self, *, timeout, headers, follow_redirects):
            captured_headers.append(headers)
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    await svc.validate_endpoint("http://llm:8000/v1", "mistral-small", api_key="secret-token")

    assert captured_headers == [{"Authorization": "Bearer secret-token"}]


@pytest.mark.asyncio
async def test_validate_endpoint_rejects_non_http_urls_without_request(monkeypatch):
    import httpx

    svc = _make_service()

    def fail_client(**_kwargs):
        raise AssertionError("HTTP client should not be created for invalid URLs")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    result = await svc.validate_endpoint("file:///etc/passwd", "mistral-small")

    assert result == {
        "reachable": False,
        "model_found": None,
        "models_served": None,
        "transcription_supported": None,
        "detail": "Endpoint URL must be an absolute HTTP(S) URL.",
    }


@pytest.mark.asyncio
async def test_validate_endpoint_rejects_malformed_urls_without_request(monkeypatch):
    import httpx

    svc = _make_service()

    def fail_client(**_kwargs):
        raise AssertionError("HTTP client should not be created for malformed URLs")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    result = await svc.validate_endpoint("http://[::1", "mistral-small")

    assert result == {
        "reachable": False,
        "model_found": None,
        "models_served": None,
        "transcription_supported": None,
        "detail": "Endpoint URL must be an absolute HTTP(S) URL.",
    }


@pytest.mark.asyncio
async def test_validate_endpoint_rejects_url_credentials_without_request(monkeypatch):
    import httpx

    svc = _make_service()

    def fail_client(**_kwargs):
        raise AssertionError("HTTP client should not be created for URLs with credentials")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    result = await svc.validate_endpoint("https://user:pass@example.test/v1", "mistral-small")

    assert result == {
        "reachable": False,
        "model_found": None,
        "models_served": None,
        "transcription_supported": None,
        "detail": "Endpoint URL must not include credentials.",
    }
