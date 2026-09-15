"""Unit tests for PartitionService preset resolution (Phase 14G)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_IDX_CONFIG = {
    "chunking": {"name": "recursive_splitter", "chunk_size": 512, "chunk_overlap_rate": 0.2},
    "parsing_strategy": "marker",
}
_RET_CONFIG = {"type": "single", "top_k": 50, "top_n": 10}


def _full_row(partition: str, **overrides) -> dict:
    base = {
        "partition": partition,
        "description": "",
        "embedder": "default",
        "indexation_preset": "default",
        "retrieval_preset": "default",
        "dimension": 1024,
        "collection_name": None,
        "chat_history_depth": 0,
        "chat_llm": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


class _FakePartitionRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._store: dict[str, dict] = {r["partition"]: r for r in (rows or [])}
        self._counts: dict[str, int] = {}
        self.calls: list[tuple[str, tuple]] = []

    async def partition_exists(self, name: str) -> bool:
        return name in self._store

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        self.calls.append(("create_partition", (name, user_id, max_owned)))
        self._store.setdefault(name, _full_row(name))
        return self._store[name]

    async def get_partition_row(self, name: str) -> dict | None:
        return self._store.get(name)

    async def list_partition_rows(self) -> list[dict]:
        return list(self._store.values())

    async def update_partition(self, name: str, **fields) -> dict | None:
        self.calls.append(("update_partition", (name,)))
        row = self._store.get(name)
        if row is None:
            return None
        row.update(fields)
        return row

    async def delete_partition(self, name: str) -> bool:
        return self._store.pop(name, None) is not None

    async def get_partition_file_count(self, partition: str) -> int:
        return self._counts.get(partition, 0)

    async def count_files_by_partition(self) -> dict[str, int]:
        return dict(self._counts)


class _FakeVectorStore:
    def __init__(self, dimension: int | None = 768) -> None:
        self._dimension = dimension

    async def collection_exists(self, name: str) -> bool:
        return False

    async def vector_dimension(self) -> int | None:
        return self._dimension


def _settings(idx=None, ret=None, embedders=("default",)):
    from core.config.model_endpoints import ModelEndpointConfig
    from core.config.root import Settings

    s = Settings()
    s.presets.indexation.clear()
    s.presets.indexation.update(idx if idx is not None else {"default": _IDX_CONFIG})
    s.presets.retrieval.clear()
    s.presets.retrieval.update(ret if ret is not None else {"default": _RET_CONFIG})
    # A partition create always assigns embedder="default" (the alias
    # ModelEndpointService files the is_default row under), and that assignment
    # is validated — so the catalog has to hold it for the create to succeed.
    s.models.embedder.update({n: ModelEndpointConfig(endpoint="http://emb:8000/v1") for n in embedders})
    return s


def _make_service(repo=None, rows=None, settings=None):
    from services.orchestrators.partition_service import PartitionService

    return PartitionService(
        partition_repo=repo or _FakePartitionRepo(rows),
        membership_repo=object(),
        document_repo=object(),
        vector_store=_FakeVectorStore(),
        user_repo=object(),
        collection="vdb",
        config=settings if settings is not None else _settings(),
    )


# ------------------------------------------------------------------
# resolve_partition_row
# ------------------------------------------------------------------


def test_resolve_partition_row_builds_config():
    from core.config.indexation_pipeline import IndexationPipelineConfig
    from core.config.retrieval_pipeline import RetrievalPipelineConfig

    svc = _make_service()
    cfg = svc.resolve_partition_row(_full_row("p1", description="hello"))

    assert cfg.name == "p1"
    assert cfg.description == "hello"
    assert cfg.embedder == "default"
    assert isinstance(cfg.indexation, IndexationPipelineConfig)
    assert isinstance(cfg.retrieval, RetrievalPipelineConfig)
    assert cfg.retrieval.top_k == 50


def test_resolve_partition_row_normalizes_legacy_zero_chat_history_depth():
    """Rows written under the old '0 = inherit global default' scheme resolve to
    the concrete default (4) instead of the no-longer-valid 0 (schema now
    requires chat_history_depth >= 1 on new writes)."""
    svc = _make_service()
    cfg = svc.resolve_partition_row(_full_row("p1", chat_history_depth=0))

    assert cfg.chat_history_depth == 4


def test_resolve_partition_row_keeps_explicit_chat_history_depth():
    svc = _make_service()
    cfg = svc.resolve_partition_row(_full_row("p1", chat_history_depth=10))

    assert cfg.chat_history_depth == 10


def test_resolve_partition_row_legacy_zero_tracks_current_global_default():
    """The legacy-0 fallback reads the live config, not a hardcoded constant —
    changing rag.chat_history_depth must change what a legacy-0 row resolves to."""
    from core.config.retrieval import RAGConfig

    settings = _settings().model_copy(update={"rag": RAGConfig(chat_history_depth=9)})
    svc = _make_service(settings=settings)

    cfg = svc.resolve_partition_row(_full_row("p1", chat_history_depth=0))

    assert cfg.chat_history_depth == 9


@pytest.mark.parametrize("global_depth", [0, -1])
def test_resolve_partition_row_legacy_zero_clamps_invalid_global_default(global_depth):
    """RAGConfig.chat_history_depth carries no lower bound, so a deployment may set it
    to 0 (or negative). A legacy-0 row would then inherit that value and hit
    PartitionConfig's ge=1 guard, crashing load_partitions() at startup. The fallback
    must clamp such values to the hardcoded default instead of propagating them."""
    from core.config.retrieval import RAGConfig

    settings = _settings().model_copy(update={"rag": RAGConfig(chat_history_depth=global_depth)})
    svc = _make_service(settings=settings)

    cfg = svc.resolve_partition_row(_full_row("p1", chat_history_depth=0))

    assert cfg.chat_history_depth == 4


def test_resolve_partition_row_missing_indexation_preset_raises():
    from core.utils.exceptions import ConfigError

    svc = _make_service()
    with pytest.raises(ConfigError, match="Indexation preset 'ghost'"):
        svc.resolve_partition_row(_full_row("p1", indexation_preset="ghost"))


def test_resolve_partition_row_missing_retrieval_preset_raises():
    from core.utils.exceptions import ConfigError

    svc = _make_service()
    with pytest.raises(ConfigError, match="Retrieval preset 'ghost'"):
        svc.resolve_partition_row(_full_row("p1", retrieval_preset="ghost"))


def test_resolve_partition_row_without_config_raises():
    from core.utils.exceptions import ConfigError
    from services.orchestrators.partition_service import PartitionService

    svc = PartitionService(
        partition_repo=_FakePartitionRepo(),
        membership_repo=object(),
        document_repo=object(),
        vector_store=object(),
        user_repo=object(),
        collection="vdb",
    )
    with pytest.raises(ConfigError, match="without a config"):
        svc.resolve_partition_row(_full_row("p1"))


# ------------------------------------------------------------------
# load_partitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_partitions_populates_cache():
    settings = _settings()
    repo = _FakePartitionRepo(rows=[_full_row("a"), _full_row("b")])
    svc = _make_service(repo, settings=settings)

    await svc.load_partitions()

    assert set(settings.partitions) == {"a", "b"}


@pytest.mark.asyncio
async def test_load_partitions_clears_stale():
    settings = _settings()
    settings.partitions["stale"] = object()  # type: ignore[assignment]
    repo = _FakePartitionRepo(rows=[_full_row("fresh")])
    svc = _make_service(repo, settings=settings)

    await svc.load_partitions()

    assert "stale" not in settings.partitions
    assert "fresh" in settings.partitions


# ------------------------------------------------------------------
# seed_default_partition
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_default_partition_creates_when_missing():
    repo = _FakePartitionRepo()
    svc = _make_service(repo)

    await svc.seed_default_partition()

    assert await repo.partition_exists("default")
    assert any(c[0] == "create_partition" for c in repo.calls)


@pytest.mark.asyncio
async def test_seed_default_partition_skips_when_present():
    repo = _FakePartitionRepo(rows=[_full_row("default")])
    svc = _make_service(repo)

    await svc.seed_default_partition()

    assert not any(c[0] == "create_partition" for c in repo.calls)


# ------------------------------------------------------------------
# create_partition (Phase 14 flow)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_partition_validates_presets_before_write():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo()
    svc = _make_service(repo)

    # User-supplied preset name → 422 ValidationError, not a 500 ConfigError.
    with pytest.raises(ValidationError, match="Indexation preset 'nope'") as exc:
        await svc.create_partition("p1", user_id=1, indexation_preset="nope")
    assert exc.value.status_code == 422

    assert not await repo.partition_exists("p1")


@pytest.mark.asyncio
async def test_create_partition_persists_config_and_reloads():
    settings = _settings()
    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=settings)

    await svc.create_partition("p1", user_id=1, description="docs")

    assert any(c[0] == "update_partition" for c in repo.calls)
    assert "p1" in settings.partitions
    assert settings.partitions["p1"].description == "docs"


@pytest.mark.asyncio
async def test_delete_partition_removes_deleted_partition_from_cache():
    settings = _settings()
    repo = _FakePartitionRepo(rows=[_full_row("p1"), _full_row("keep")])
    svc = _make_service(repo, settings=settings)
    await svc.load_partitions()

    await svc.delete_partition("p1")

    assert "p1" not in settings.partitions
    assert "keep" in settings.partitions


# ------------------------------------------------------------------
# update_partition
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_partition_validates_and_reloads():
    settings = _settings()
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo, settings=settings)

    await svc.update_partition("p1", description="new")

    assert repo._store["p1"]["description"] == "new"
    assert settings.partitions["p1"].description == "new"


@pytest.mark.asyncio
async def test_update_partition_rejects_unknown_preset():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)

    with pytest.raises(ValidationError, match="Retrieval preset 'ghost'") as exc:
        await svc.update_partition("p1", retrieval_preset="ghost")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_partition_missing_raises_404():
    from core.utils.exceptions import PartitionNotFoundError

    svc = _make_service(_FakePartitionRepo())
    with pytest.raises(PartitionNotFoundError):
        await svc.update_partition("ghost", description="x")


# ------------------------------------------------------------------
# chat_llm assignment (model-endpoint reference)
# ------------------------------------------------------------------


def _settings_with_llm(*names: str):
    from core.config.model_endpoints import ModelEndpointConfig

    s = _settings()
    s.models.llm.update({n: ModelEndpointConfig(endpoint="http://llm:8000/v1") for n in names})
    return s


@pytest.mark.asyncio
async def test_update_partition_rejects_unknown_chat_llm():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo, settings=_settings_with_llm("mistral"))

    with pytest.raises(ValidationError, match="LLM endpoint 'ghost'") as exc:
        await svc.update_partition("p1", chat_llm="ghost")
    assert exc.value.status_code == 422
    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert repo._store["p1"]["chat_llm"] is None  # nothing was written


@pytest.mark.asyncio
async def test_update_partition_accepts_catalogued_chat_llm():
    settings = _settings_with_llm("mistral")
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo, settings=settings)

    await svc.update_partition("p1", chat_llm="mistral")

    assert repo._store["p1"]["chat_llm"] == "mistral"
    assert settings.partitions["p1"].chat_llm == "mistral"


@pytest.mark.asyncio
async def test_update_partition_explicit_none_clears_chat_llm():
    # The UI resets to the default LLM by PATCHing chat_llm=null — the
    # None-filter that gives other columns partial-PATCH semantics must
    # not swallow it.
    settings = _settings_with_llm("mistral")
    repo = _FakePartitionRepo(rows=[_full_row("p1", chat_llm="mistral")])
    svc = _make_service(repo, settings=settings)

    await svc.update_partition("p1", chat_llm=None)

    assert repo._store["p1"]["chat_llm"] is None
    assert settings.partitions["p1"].chat_llm is None


@pytest.mark.asyncio
async def test_update_partition_stale_stored_chat_llm_does_not_block_other_updates():
    # Endpoint deleted after assignment: the stored name is stale, but a
    # PATCH that doesn't touch chat_llm must still succeed (runtime falls
    # back to the default LLM for the stale name).
    repo = _FakePartitionRepo(rows=[_full_row("p1", chat_llm="deleted-endpoint")])
    svc = _make_service(repo, settings=_settings_with_llm("mistral"))

    await svc.update_partition("p1", description="new")

    assert repo._store["p1"]["description"] == "new"
    assert repo._store["p1"]["chat_llm"] == "deleted-endpoint"


@pytest.mark.asyncio
async def test_create_partition_rejects_unknown_chat_llm():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=_settings_with_llm("mistral"))

    with pytest.raises(ValidationError, match="LLM endpoint 'ghost'") as exc:
        await svc.create_partition("p1", user_id=1, chat_llm="ghost")
    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert not await repo.partition_exists("p1")


@pytest.mark.asyncio
async def test_create_partition_accepts_catalogued_chat_llm():
    settings = _settings_with_llm("mistral")
    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=settings)

    await svc.create_partition("p1", user_id=1, chat_llm="mistral")

    assert repo._store["p1"]["chat_llm"] == "mistral"
    assert settings.partitions["p1"].chat_llm == "mistral"


# ------------------------------------------------------------------
# embedder assignment (model-endpoint reference)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_partition_rejects_unknown_embedder():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo, settings=_settings(embedders=("default", "bge-m3")))

    with pytest.raises(ValidationError, match="Embedder endpoint 'bge-m4'") as exc:
        await svc.update_partition("p1", embedder="bge-m4")
    assert exc.value.status_code == 422
    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert repo._store["p1"]["embedder"] == "default"  # nothing was written


@pytest.mark.asyncio
async def test_update_partition_accepts_catalogued_embedder():
    settings = _settings(embedders=("default", "bge-m3"))
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo, settings=settings)

    await svc.update_partition("p1", embedder="bge-m3")

    assert repo._store["p1"]["embedder"] == "bge-m3"
    assert settings.partitions["p1"].embedder == "bge-m3"


@pytest.mark.asyncio
async def test_update_partition_stale_stored_embedder_does_not_block_other_updates():
    # Unlike chat_llm a stale embedder has no runtime fallback, but a PATCH
    # that doesn't touch it still must not be held hostage by it — otherwise
    # the partition becomes uneditable, including the rename that would fix it.
    repo = _FakePartitionRepo(rows=[_full_row("p1", embedder="deleted-endpoint")])
    svc = _make_service(repo, settings=_settings())

    await svc.update_partition("p1", description="new")

    assert repo._store["p1"]["description"] == "new"
    assert repo._store["p1"]["embedder"] == "deleted-endpoint"


@pytest.mark.asyncio
async def test_create_partition_rejects_unknown_embedder():
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=_settings())

    with pytest.raises(ValidationError, match="Embedder endpoint 'ghost'") as exc:
        await svc.create_partition("p1", user_id=1, embedder="ghost")
    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert not await repo.partition_exists("p1")


@pytest.mark.asyncio
async def test_create_partition_accepts_catalogued_embedder():
    settings = _settings(embedders=("default", "bge-m3"))
    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=settings)

    await svc.create_partition("p1", user_id=1, embedder="bge-m3")

    assert repo._store["p1"]["embedder"] == "bge-m3"
    assert settings.partitions["p1"].embedder == "bge-m3"


@pytest.mark.asyncio
async def test_create_partition_rejects_default_embedder_when_none_is_catalogued():
    """ "default" is the alias for the is_default row, not a free pass: with no
    embedder endpoint registered there is nothing to index with, so the create
    fails here instead of at the first upload."""
    from core.utils.exceptions import ValidationError

    repo = _FakePartitionRepo()
    svc = _make_service(repo, settings=_settings(embedders=()))

    with pytest.raises(ValidationError, match="Embedder endpoint 'default'"):
        await svc.create_partition("p1", user_id=1)
    assert not await repo.partition_exists("p1")


# ------------------------------------------------------------------
# get_partition_config / update_partition_config (PartitionDetailResponse)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_partition_config_returns_resolved_detail():
    repo = _FakePartitionRepo(rows=[_full_row("p1", description="docs")])
    repo._counts["p1"] = 7
    svc = _make_service(repo)

    detail = await svc.get_partition_config("p1")

    assert detail["name"] == "p1"
    assert detail["description"] == "docs"
    assert detail["document_count"] == 7
    assert detail["embedder"] == "default"
    assert detail["indexation_preset"] == "default"
    assert detail["retrieval_preset"] == "default"
    # The row says 1024 (the column's server default, which nothing writes);
    # the live collection says 768. The API must report the collection.
    assert detail["dimension"] == 768
    assert detail["retrieval_pipeline"]["top_k"] == 50
    assert "chunking" in detail["indexation_pipeline"]
    assert "chat_history_depth" in detail
    assert "chat_llm" in detail


@pytest.mark.asyncio
async def test_list_partition_summaries_has_counts_and_no_pipelines():
    repo = _FakePartitionRepo(rows=[_full_row("p1", description="docs"), _full_row("p2")])
    repo._counts["p1"] = 4
    svc = _make_service(repo)

    summaries = await svc.list_partition_summaries()

    assert set(summaries) == {"p1", "p2"}
    assert summaries["p1"]["document_count"] == 4
    assert summaries["p2"]["document_count"] == 0
    assert summaries["p1"]["description"] == "docs"
    # lightweight: stored columns only, pipelines are resolved on detail
    assert "indexation_pipeline" not in summaries["p1"]
    assert "retrieval_pipeline" not in summaries["p1"]


@pytest.mark.asyncio
async def test_get_partition_config_missing_raises_404():
    from core.utils.exceptions import PartitionNotFoundError

    svc = _make_service(_FakePartitionRepo())
    with pytest.raises(PartitionNotFoundError):
        await svc.get_partition_config("ghost")


@pytest.mark.asyncio
async def test_update_partition_config_applies_and_returns_detail():
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)

    detail = await svc.update_partition_config("p1", description="new")

    assert detail["description"] == "new"
    assert repo._store["p1"]["description"] == "new"


# ------------------------------------------------------------------
# reported dimension (#762 G)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_dimension_is_null_when_the_store_cannot_tell():
    """No collection yet, or an unreachable Milvus. "Unknown" is a fact; the
    1024 this used to echo was a fabrication."""
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)
    svc._vector_store = _FakeVectorStore(dimension=None)

    detail = await svc.get_partition_config("p1")

    assert detail["dimension"] is None


@pytest.mark.asyncio
async def test_detail_dimension_survives_a_vector_store_failure():
    """The dimension is informational — a briefly unreachable store must not
    turn a partition-config read into a 500."""

    class _BrokenStore(_FakeVectorStore):
        async def vector_dimension(self) -> int | None:
            raise RuntimeError("milvus unreachable")

    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)
    svc._vector_store = _BrokenStore()

    detail = await svc.get_partition_config("p1")

    assert detail["dimension"] is None
    assert detail["name"] == "p1"


@pytest.mark.asyncio
async def test_list_summaries_report_the_live_dimension_once():
    """One collection serves every partition, so they share the answer — and
    the lookup is made once for the whole list, not per row."""
    calls = {"n": 0}

    class _CountingStore(_FakeVectorStore):
        async def vector_dimension(self) -> int | None:
            calls["n"] += 1
            return 768

    repo = _FakePartitionRepo(rows=[_full_row("a"), _full_row("b"), _full_row("c")])
    svc = _make_service(repo)
    svc._vector_store = _CountingStore()

    summaries = await svc.list_partition_summaries()

    assert {s["dimension"] for s in summaries.values()} == {768}
    assert calls["n"] == 1


# ------------------------------------------------------------------
# per-file embedder provenance (#762 E)
# ------------------------------------------------------------------


class _FakeDocRepoWithProvenance:
    def __init__(self, rows=None, raises=False):
        self._rows = rows or []
        self._raises = raises
        self.calls: list[str] = []

    async def count_files_by_embedder(self, partition: str):
        self.calls.append(partition)
        if self._raises:
            raise RuntimeError("catalog unreachable")
        return self._rows


@pytest.mark.asyncio
async def test_detail_reports_which_embedders_actually_indexed_the_files():
    """The partition row says what is configured now; this says what the files
    were built with. The gap between them is the drift."""
    rows = [
        {"embedder": "Qwen3-Embedding-0.6B", "model_name": "Qwen3-Embedding-0.6B", "dimension": 1024, "file_count": 8},
        {"embedder": "bge-m3", "model_name": "bge-m3", "dimension": 1024, "file_count": 3},
    ]
    repo = _FakePartitionRepo(rows=[_full_row("p1", embedder="bge-m3")])
    svc = _make_service(repo, settings=_settings(embedders=("default", "bge-m3")))
    svc._document_repo = _FakeDocRepoWithProvenance(rows)

    detail = await svc.get_partition_config("p1")

    assert detail["embedder"] == "bge-m3"  # configured now
    assert [r["file_count"] for r in detail["indexed_embedders"]] == [8, 3]
    # 8 files predate the swap and are the ones a repair would scope to.
    assert detail["indexed_embedders"][0]["embedder"] == "Qwen3-Embedding-0.6B"


@pytest.mark.asyncio
async def test_detail_keeps_pre_provenance_files_as_unknown():
    """Not backfilled with the current setting — that would be a guess dressed
    as a record, and wrong for exactly the files worth finding."""
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)
    svc._document_repo = _FakeDocRepoWithProvenance(
        [{"embedder": None, "model_name": None, "dimension": None, "file_count": 5}]
    )

    detail = await svc.get_partition_config("p1")

    assert detail["indexed_embedders"] == [{"embedder": None, "model_name": None, "dimension": None, "file_count": 5}]


@pytest.mark.asyncio
async def test_detail_survives_a_catalog_failure():
    repo = _FakePartitionRepo(rows=[_full_row("p1")])
    svc = _make_service(repo)
    svc._document_repo = _FakeDocRepoWithProvenance(raises=True)

    detail = await svc.get_partition_config("p1")

    assert detail["indexed_embedders"] == []
    assert detail["name"] == "p1"
