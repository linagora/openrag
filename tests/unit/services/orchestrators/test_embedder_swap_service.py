"""Unit tests for :class:`EmbedderSwapService` (#762 F4)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.config.root import Settings
from core.utils.exceptions import ConflictError, NotFoundError, ValidationError, VDBInsertError
from services.orchestrators.embedder_swap_service import EmbedderSwapService

SOURCE_FIELD = "vector_e5"
TARGET_FIELD = "vector_bge_m3"


class FakePartitionRepo:
    def __init__(self, *, swap: dict | None = None, claimed: bool = True) -> None:
        self.rows = {"p1": {"partition": "p1", "embedder": "e5"}}
        self.swap = swap
        self.claimed = claimed
        self.partition_updates: list[tuple[str, dict]] = []

    async def get_partition_row(self, name: str) -> dict | None:
        return self.rows.get(name)

    async def update_partition(self, name: str, **fields) -> dict | None:
        self.partition_updates.append((name, fields))
        self.rows[name].update(fields)
        return self.rows[name]

    async def start_embedder_swap(self, partition, *, source_embedder, target_embedder, files_total):
        if self.swap is not None and self.swap["status"] == "running":
            return None
        now = datetime(2026, 9, 14, tzinfo=UTC)
        self.swap = {
            "partition": partition,
            "source_embedder": source_embedder,
            "target_embedder": target_embedder,
            "status": "running",
            "files_total": files_total,
            "files_done": 0,
            "error": None,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        return dict(self.swap)

    async def get_embedder_swap(self, partition: str) -> dict | None:
        return dict(self.swap) if self.swap is not None else None

    async def list_embedder_swaps(self, status: str | None = None) -> list[dict]:
        if self.swap is None or (status is not None and self.swap["status"] != status):
            return []
        return [dict(self.swap)]

    async def update_embedder_swap(self, partition: str, **fields) -> dict | None:
        if self.swap is None or self.swap["status"] != "running":
            return None
        self.swap.update(fields)
        return dict(self.swap)

    async def complete_embedder_swap(self, partition: str) -> dict | None:
        if self.swap is None or self.swap["status"] != "running":
            return None
        self.swap.update(status="completed", finished_at=datetime(2026, 9, 14, 1, tzinfo=UTC))
        await self.update_partition(partition, embedder=self.swap["target_embedder"])
        return dict(self.swap)

    @asynccontextmanager
    async def embedder_swap_runner_lock(self, partition: str):
        yield self.claimed


class FakeDocumentRepo:
    def __init__(self, files: list[dict]) -> None:
        self.files = files
        self.recorded: dict[str, dict] = {}

    async def list_file_embedders(self, partition: str) -> list[dict]:
        return [dict(f) for f in self.files]

    async def record_file_embedder(self, file_id: str, partition: str, provenance: dict) -> bool:
        self.recorded[file_id] = provenance
        return True


class FakeVectorStore:
    def __init__(self, chunks: dict[str, list[dict]]) -> None:
        self.chunks = chunks
        self.ensured: dict[str, int] = {}
        self.written: dict[str, dict[str, list[float] | None]] = {}
        self.write_failures: list[Exception] = []

    async def query_chunks_by_filter(self, collection, filters, output_fields=None):
        return [dict(row) for row in self.chunks.get(filters["file_id"], [])]

    async def ensure_vector_field(self, field: str, dimension: int) -> bool:
        self.ensured[field] = dimension
        return True

    async def write_vectors(self, field: str, vectors: dict) -> int:
        if self.write_failures:
            raise self.write_failures.pop(0)
        self.written.setdefault(field, {}).update(vectors)
        return len(vectors)


class FakeEmbedder:
    model_name = "BAAI/bge-m3"
    endpoint = "http://bge:8000/v1"
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return [[float(len(text)), 1.0] for text in texts]


class FakePartitionService:
    def __init__(self) -> None:
        self.locks: list[str] = []
        self.reloads = 0

    async def partition_exists(self, partition: str) -> bool:
        return partition == "p1"

    @asynccontextmanager
    async def operation_lock(self, partition: str):
        self.locks.append(partition)
        yield

    async def load_partitions(self) -> None:
        self.reloads += 1


def _settings() -> Settings:
    settings = Settings()
    settings.models.embedder.update(
        {
            "e5": ModelEndpointConfig(endpoint="http://e5:8000/v1", vector_field=SOURCE_FIELD),
            "bge-m3": ModelEndpointConfig(endpoint="http://bge:8000/v1", vector_field=TARGET_FIELD),
            "default": ModelEndpointConfig(endpoint="http://e5:8000/v1", vector_field=SOURCE_FIELD),
        }
    )
    return settings


def _make(files=None, chunks=None, *, repo=None, task_count=0, monkeypatch=None):
    files = (
        files
        if files is not None
        else [
            {"file_id": "a", "embedder_model_name": "intfloat/e5", "embedder_vector_field": SOURCE_FIELD},
            {"file_id": "b", "embedder_model_name": "intfloat/e5", "embedder_vector_field": SOURCE_FIELD},
        ]
    )
    chunks = (
        chunks
        if chunks is not None
        else {
            "a": [{"_id": 1, "text": "one"}, {"_id": 2, "text": "three"}],
            "b": [{"_id": 3, "text": "hello"}],
        }
    )
    embedder = FakeEmbedder()
    parts = {
        "repo": repo or FakePartitionRepo(),
        "documents": FakeDocumentRepo(files),
        "store": FakeVectorStore(chunks),
        "partitions": FakePartitionService(),
        "embedder": embedder,
        "factory_calls": [],
    }

    def factory(name: str):
        parts["factory_calls"].append(name)
        return embedder

    service = EmbedderSwapService(
        partition_repo=parts["repo"],
        document_repo=parts["documents"],
        vector_store=parts["store"],
        partition_service=parts["partitions"],
        config=_settings(),
        embedder_factory=factory,
        collection="vdb",
        task_state_manager_factory=lambda: object(),
    )

    async def count(_tsm, *, partition, timeout):
        return task_count

    if monkeypatch is not None:
        monkeypatch.setattr("services.orchestrators.embedder_swap_service.count_active_indexing_tasks", count)
    return service, parts


async def _finish(service: EmbedderSwapService, partition: str = "p1") -> None:
    job = service._jobs.get(partition)
    if job is not None:
        await job


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def test_each_file_is_re_embedded_from_its_stored_text_into_the_target_field(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)

    swap = await service.start("p1", "bge-m3")
    await _finish(service)

    assert swap["status"] == "running"
    assert swap["started_at"] == "2026-09-14T00:00:00+00:00"
    assert parts["factory_calls"] == ["bge-m3"]
    # The chunk text as stored: nothing is re-parsed, re-chunked or re-contextualized.
    assert parts["embedder"].calls == [["one", "three"], ["hello"]]
    assert parts["store"].ensured == {TARGET_FIELD: 2}
    assert parts["store"].written[TARGET_FIELD] == {"1": [3.0, 1.0], "2": [5.0, 1.0], "3": [5.0, 1.0]}


async def test_each_file_records_the_model_and_field_it_now_sits_in(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["documents"].recorded["a"] == {
        "embedder": "bge-m3",
        "embedder_model_name": "BAAI/bge-m3",
        "embedder_endpoint": "http://bge:8000/v1",
        "embedder_dimension": 2,
        "embedder_vector_field": TARGET_FIELD,
    }
    assert set(parts["documents"].recorded) == {"a", "b"}


async def test_completion_points_the_partition_at_the_target_under_the_partition_fence(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    swap = parts["repo"].swap
    assert swap["status"] == "completed"
    assert swap["files_done"] == swap["files_total"] == 2
    assert swap["finished_at"] is not None
    assert parts["repo"].rows["p1"]["embedder"] == "bge-m3"
    # Start and completion both take the fence uploads are admitted under.
    assert parts["partitions"].locks == ["p1", "p1"]
    assert parts["partitions"].reloads == 1


async def test_the_old_fields_vectors_are_left_alone(monkeypatch):
    """Clearing them once the partition unlocks would race a swap straight back
    to that embedder, which writes into the same field."""
    service, parts = _make(monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert list(parts["store"].written) == [TARGET_FIELD]


async def test_a_swap_cancelled_as_it_completes_leaves_the_partition_on_its_embedder(monkeypatch):
    repo = FakePartitionRepo()
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)
    original_lock = parts["partitions"].operation_lock

    @asynccontextmanager
    async def lock_then_cancel(partition):
        async with original_lock(partition):
            if repo.swap is not None and repo.swap["files_done"] == repo.swap["files_total"]:
                repo.swap["status"] = "cancelled"
            yield

    parts["partitions"].operation_lock = lock_then_cancel

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert repo.swap["status"] == "cancelled"
    assert repo.rows["p1"]["embedder"] == "e5"
    assert parts["partitions"].reloads == 0


async def test_files_already_in_the_target_space_are_skipped(monkeypatch):
    """What makes a restart resumable, and a swap onto the current embedder a
    drift repair: only files recorded with another model or field are redone."""
    files = [
        {"file_id": "a", "embedder_model_name": "BAAI/bge-m3", "embedder_vector_field": TARGET_FIELD},
        # Right model, wrong field: an endpoint repointed at bge-m3 keeps its own field.
        {"file_id": "b", "embedder_model_name": "BAAI/bge-m3", "embedder_vector_field": SOURCE_FIELD},
    ]
    service, parts = _make(files, monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["embedder"].calls == [["hello"]]
    assert set(parts["documents"].recorded) == {"b"}
    assert parts["repo"].swap["files_done"] == 2


async def test_a_swap_onto_the_embedder_the_partition_already_uses_completes_in_place(monkeypatch):
    repo = FakePartitionRepo()
    repo.rows["p1"]["embedder"] = "bge-m3"
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["repo"].swap["status"] == "completed"
    assert repo.rows["p1"]["embedder"] == "bge-m3"


async def test_a_file_with_no_chunks_is_recorded_so_a_resume_skips_it(monkeypatch):
    service, parts = _make(chunks={"a": [], "b": []}, monkeypatch=monkeypatch)

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["embedder"].calls == []
    assert set(parts["documents"].recorded) == {"a", "b"}
    assert parts["repo"].swap["status"] == "completed"


async def test_chunks_deleted_during_the_write_are_dropped_and_the_rest_written(monkeypatch):
    """Deleting files stays allowed during a swap, and Milvus refuses a whole
    partial update when one chunk no longer exists."""
    service, parts = _make(monkeypatch=monkeypatch)
    store = parts["store"]
    store.write_failures = [VDBInsertError("partial update requires every primary key to exist")]
    original_query = store.query_chunks_by_filter
    calls = {"a": 0}

    async def query(collection, filters, output_fields=None):
        rows = await original_query(collection, filters, output_fields)
        if filters["file_id"] == "a":
            calls["a"] += 1
            if calls["a"] > 1:  # chunk 2 was deleted after the first read
                return [row for row in rows if row["_id"] != 2]
        return rows

    store.query_chunks_by_filter = query

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert store.written[TARGET_FIELD] == {"1": [3.0, 1.0], "3": [5.0, 1.0]}
    assert parts["repo"].swap["status"] == "completed"


async def test_a_refused_write_with_nothing_deleted_fails_the_swap(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["store"].write_failures = [VDBInsertError("milvus is down")]

    await service.start("p1", "bge-m3")
    await _finish(service)

    swap = parts["repo"].swap
    assert swap["status"] == "failed"
    assert "milvus is down" in swap["error"]
    assert parts["repo"].partition_updates == []


async def test_an_embedder_failure_fails_the_swap_and_keeps_the_partition(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["embedder"].error = RuntimeError("vLLM unreachable")

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["repo"].swap["status"] == "failed"
    assert parts["repo"].swap["error"] == "vLLM unreachable"
    assert parts["repo"].rows["p1"]["embedder"] == "e5"


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------


async def test_a_swap_does_not_start_while_files_are_being_indexed(monkeypatch):
    """Their files would be written into the old field after the job listed them."""
    service, parts = _make(task_count=2, monkeypatch=monkeypatch)

    with pytest.raises(ConflictError) as exc:
        await service.start("p1", "bge-m3")

    assert exc.value.code == "INDEXING_IN_PROGRESS"
    assert parts["repo"].swap is None


async def test_a_second_swap_is_refused_while_one_runs(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["embedder"].gate = asyncio.Event()
    await service.start("p1", "bge-m3")

    with pytest.raises(ConflictError) as exc:
        await service.start("p1", "bge-m3")

    assert exc.value.code == "EMBEDDER_SWAP_IN_PROGRESS"
    parts["embedder"].gate.set()
    await _finish(service)


@pytest.mark.parametrize(
    ("target", "code"),
    [("default", "EMBEDDER_ALIAS_NOT_ALLOWED"), ("bge-m4", "MODEL_ENDPOINT_NOT_FOUND")],
)
async def test_the_target_must_name_a_catalogued_embedder(monkeypatch, target, code):
    service, parts = _make(monkeypatch=monkeypatch)

    with pytest.raises(ValidationError) as exc:
        await service.start("p1", target)

    assert exc.value.code == code
    assert parts["repo"].swap is None


# ---------------------------------------------------------------------------
# Cancelling, resuming, observing
# ---------------------------------------------------------------------------


async def test_cancel_stops_the_job_and_the_partition_keeps_its_embedder(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["embedder"].gate = asyncio.Event()
    await service.start("p1", "bge-m3")
    await asyncio.sleep(0)

    swap = await service.cancel("p1")

    assert swap["status"] == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await service._jobs["p1"]
    assert parts["repo"].rows["p1"]["embedder"] == "e5"
    assert parts["repo"].partition_updates == []


async def test_a_swap_cancelled_by_another_process_stops_at_the_next_file(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    original_embed = parts["embedder"].embed

    async def embed_then_cancel_elsewhere(texts):
        parts["repo"].swap["status"] = "cancelled"
        return await original_embed(texts)

    parts["embedder"].embed = embed_then_cancel_elsewhere

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["embedder"].calls == [["one", "three"]]
    assert parts["repo"].partition_updates == []


async def test_cancelling_nothing_is_a_conflict(monkeypatch):
    service, _ = _make(monkeypatch=monkeypatch)

    with pytest.raises(ConflictError) as exc:
        await service.cancel("p1")

    assert exc.value.code == "EMBEDDER_SWAP_NOT_RUNNING"


async def test_running_swaps_resume_at_startup(monkeypatch):
    repo = FakePartitionRepo()
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    assert await service.resume_running() == 1
    await _finish(service)

    assert repo.swap["status"] == "completed"


async def test_a_swap_another_process_runs_is_left_to_it(monkeypatch):
    repo = FakePartitionRepo(claimed=False)
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    await service.resume_running()
    await _finish(service)

    assert parts["embedder"].calls == []
    assert repo.swap["status"] == "running"


async def test_reading_a_running_swap_restarts_a_stranded_job(monkeypatch):
    """Its runner died: polling it is enough to get it going again."""
    repo = FakePartitionRepo()
    service, _ = _make(repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    await service.get("p1")
    await _finish(service)

    assert repo.swap["status"] == "completed"


async def test_a_partition_that_never_swapped_has_no_swap(monkeypatch):
    service, _ = _make(monkeypatch=monkeypatch)

    with pytest.raises(NotFoundError):
        await service.get("p1")


async def test_shutdown_stops_jobs_without_ending_their_swaps(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["embedder"].gate = asyncio.Event()
    await service.start("p1", "bge-m3")
    await asyncio.sleep(0)

    await service.shutdown()

    assert parts["repo"].swap["status"] == "running"
