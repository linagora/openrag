"""Unit tests for PresetService (Phase 14F)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_VALID_IDX_CONFIG = {
    "chunking": {"name": "recursive_splitter", "chunk_size": 512, "chunk_overlap_rate": 0.2},
    "parsing_strategy": "marker",
    "enable_image_captioning": True,
}

_VALID_RET_CONFIG = {
    "type": "single",
    "top_k": 50,
    "top_n": 10,
    "enable_reranker": True,
    "similarity_threshold": 0.6,
}


def _make_row(name: str, preset_type: str, config: dict | None = None) -> dict:
    return {
        "name": name,
        "preset_type": preset_type,
        "config": config or _VALID_IDX_CONFIG,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


class _FakePresetRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._store: dict[tuple[str, str], dict] = {}
        self._partition_counts: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, tuple]] = []
        for r in rows or []:
            self._store[(r["name"], r["preset_type"])] = r

    async def get(self, name: str, preset_type: str) -> dict | None:
        self.calls.append(("get", (name, preset_type)))
        return self._store.get((name, preset_type))

    async def list_all(self, preset_type: str | None = None) -> list[dict]:
        self.calls.append(("list_all", (preset_type,)))
        rows = list(self._store.values())
        if preset_type is not None:
            rows = [r for r in rows if r["preset_type"] == preset_type]
        return rows

    async def upsert(self, name: str, preset_type: str, config: dict) -> dict:
        self.calls.append(("upsert", (name, preset_type)))
        row = _make_row(name, preset_type, config)
        self._store[(name, preset_type)] = row
        return row

    async def delete(self, name: str, preset_type: str) -> bool:
        self.calls.append(("delete", (name, preset_type)))
        return self._store.pop((name, preset_type), None) is not None

    async def count_partitions_using(self, name: str, preset_type: str) -> int:
        self.calls.append(("count_partitions_using", (name, preset_type)))
        return self._partition_counts.get((name, preset_type), 0)


class _FakePartitionService:
    def __init__(self) -> None:
        self.load_calls = 0

    async def load_partitions(self) -> None:
        self.load_calls += 1


def _make_service(repo=None, rows=None, settings=None, partition_service=None):
    from core.config.root import Settings
    from services.orchestrators.preset_service import PresetService

    return PresetService(
        preset_repo=repo or _FakePresetRepo(rows),
        config=settings or Settings(),
        partition_service=partition_service,
    )


# ------------------------------------------------------------------
# seed_defaults
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_defaults_inserts_six_presets_when_empty():
    repo = _FakePresetRepo()
    svc = _make_service(repo)
    await svc.seed_defaults()

    upserts = [c for c in repo.calls if c[0] == "upsert"]
    assert len(upserts) == 6  # 3 indexation + 3 retrieval


@pytest.mark.asyncio
async def test_seed_defaults_retrieval_inherits_reranker_enabled():
    from core.config.root import Settings

    settings = Settings(reranker={"provider": "infinity", "enabled": False})
    repo = _FakePresetRepo()
    svc = _make_service(repo, settings=settings)
    await svc.seed_defaults()

    ret = [r for r in repo._store.values() if r["preset_type"] == "retrieval"]
    assert ret  # sanity: retrieval presets were seeded
    assert all(r["config"]["enable_reranker"] is False for r in ret)


@pytest.mark.asyncio
async def test_seed_defaults_retrieval_enables_reranker_when_available():
    from core.config.root import Settings

    settings = Settings(reranker={"provider": "infinity", "enabled": True})
    repo = _FakePresetRepo()
    svc = _make_service(repo, settings=settings)
    await svc.seed_defaults()

    ret = [r for r in repo._store.values() if r["preset_type"] == "retrieval"]
    assert all(r["config"]["enable_reranker"] is True for r in ret)


@pytest.mark.asyncio
async def test_seed_defaults_skips_type_when_rows_exist():
    existing = _make_row("default", "indexation")
    repo = _FakePresetRepo(rows=[existing])
    svc = _make_service(repo)
    await svc.seed_defaults()

    upserts = [c for c in repo.calls if c[0] == "upsert"]
    upserted_types = {args[1] for _, args in upserts}
    assert "indexation" not in upserted_types
    assert "retrieval" in upserted_types


# ------------------------------------------------------------------
# load_all
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_all_populates_config_presets():
    from core.config.root import Settings

    rows = [
        _make_row("default", "indexation", _VALID_IDX_CONFIG),
        _make_row("default", "retrieval", _VALID_RET_CONFIG),
    ]
    repo = _FakePresetRepo(rows=rows)
    settings = Settings()
    svc = _make_service(repo, settings=settings)

    await svc.load_all()

    assert "default" in settings.presets.indexation
    assert "default" in settings.presets.retrieval


@pytest.mark.asyncio
async def test_load_all_clears_stale_entries():
    from core.config.root import Settings

    rows = [_make_row("old", "indexation", _VALID_IDX_CONFIG)]
    repo = _FakePresetRepo(rows=rows)
    settings = Settings()
    settings.presets.indexation["stale"] = {}
    svc = _make_service(repo, settings=settings)

    await svc.load_all()

    assert "stale" not in settings.presets.indexation
    assert "old" in settings.presets.indexation


# ------------------------------------------------------------------
# create_preset
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_preset_rejects_invalid_type():
    from core.utils.exceptions import ValidationError

    svc = _make_service()
    with pytest.raises(ValidationError, match="Invalid preset_type"):
        await svc.create_preset("my-preset", "unknown_type", {})


@pytest.mark.asyncio
async def test_create_preset_raises_409_on_duplicate():
    from core.utils.exceptions import ValidationError

    existing = _make_row("default", "indexation")
    repo = _FakePresetRepo(rows=[existing])
    svc = _make_service(repo)

    with pytest.raises(ValidationError) as exc_info:
        await svc.create_preset("default", "indexation", _VALID_IDX_CONFIG)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_preset_rejects_invalid_config():
    from core.utils.exceptions import ValidationError

    svc = _make_service()
    bad_config = {"top_k": -999}  # violates gt=0 constraint
    with pytest.raises(ValidationError, match="Invalid retrieval preset config"):
        await svc.create_preset("bad", "retrieval", bad_config)


@pytest.mark.asyncio
async def test_create_preset_inserts_and_returns_row():
    repo = _FakePresetRepo()
    svc = _make_service(repo)

    result = await svc.create_preset("legal", "indexation", _VALID_IDX_CONFIG)

    assert result["name"] == "legal"
    assert result["preset_type"] == "indexation"
    assert any(c[0] == "upsert" for c in repo.calls)


# ------------------------------------------------------------------
# get_preset
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_preset_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.get_preset("ghost", "indexation")


@pytest.mark.asyncio
async def test_get_preset_returns_row_when_found():
    existing = _make_row("default", "retrieval", _VALID_RET_CONFIG)
    svc = _make_service(rows=[existing])

    row = await svc.get_preset("default", "retrieval")
    assert row["name"] == "default"
    assert row["preset_type"] == "retrieval"


# ------------------------------------------------------------------
# update_preset
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_preset_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.update_preset("ghost", "indexation", config=_VALID_IDX_CONFIG)


@pytest.mark.asyncio
async def test_update_preset_renames_preset():
    existing = _make_row("old-name", "retrieval", _VALID_RET_CONFIG)
    repo = _FakePresetRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.update_preset("old-name", "retrieval", new_name="new-name")

    assert ("old-name", "retrieval") not in repo._store
    assert ("new-name", "retrieval") in repo._store


@pytest.mark.asyncio
async def test_update_preset_calls_partition_service():
    existing = _make_row("default", "retrieval", _VALID_RET_CONFIG)
    repo = _FakePresetRepo(rows=[existing])
    part_svc = _FakePartitionService()
    svc = _make_service(repo, partition_service=part_svc)

    await svc.update_preset("default", "retrieval", config=_VALID_RET_CONFIG)

    assert part_svc.load_calls == 1


@pytest.mark.asyncio
async def test_update_preset_rejects_invalid_new_config():
    from core.utils.exceptions import ValidationError

    existing = _make_row("default", "retrieval", _VALID_RET_CONFIG)
    repo = _FakePresetRepo(rows=[existing])
    svc = _make_service(repo)

    with pytest.raises(ValidationError, match="Invalid retrieval preset config"):
        await svc.update_preset("default", "retrieval", config={"top_k": -1})


# ------------------------------------------------------------------
# delete_preset
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_preset_raises_404_when_missing():
    from core.utils.exceptions import NotFoundError

    svc = _make_service()
    with pytest.raises(NotFoundError):
        await svc.delete_preset("ghost", "indexation")


@pytest.mark.asyncio
async def test_delete_preset_raises_422_when_in_use():
    from core.utils.exceptions import ValidationError

    existing = _make_row("default", "indexation")
    repo = _FakePresetRepo(rows=[existing])
    repo._partition_counts[("default", "indexation")] = 2
    svc = _make_service(repo)

    with pytest.raises(ValidationError, match="2 partition"):
        await svc.delete_preset("default", "indexation")


@pytest.mark.asyncio
async def test_delete_preset_removes_row():
    existing = _make_row("legal", "indexation")
    repo = _FakePresetRepo(rows=[existing])
    svc = _make_service(repo)

    await svc.delete_preset("legal", "indexation")

    assert ("legal", "indexation") not in repo._store
