"""Unit tests for :class:`EmbedderSwapService`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.config.root import Settings
from core.utils.exceptions import ConflictError, ValidationError, VDBInsertError, VDBSearchError
from services.orchestrators.embedder_swap_service import EmbedderSwapService

SOURCE_FIELD = "vector_e5"
TARGET_FIELD = "vector_bge_m3"


class FakePartitionRepo:
    def __init__(self, *, swap: dict | None = None, claimed: bool = True) -> None:
        self.rows = {"p1": {"partition": "p1", "embedder": "e5"}}
        self.swap = swap
        self.claimed = claimed
        self.partition_updates: list[tuple[str, dict]] = []
        self.runs = 0

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
        self.runs += 1
        self.swap = {
            "partition": partition,
            "source_embedder": source_embedder,
            "target_embedder": target_embedder,
            "status": "running",
            "files_total": files_total,
            "files_done": 0,
            "chunks_over_window": 0,
            "error": None,
            "run_id": f"run-{self.runs}",
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

    async def update_embedder_swap(self, partition: str, *, run_id=None, **fields) -> dict | None:
        if self.swap is None or self.swap["status"] != "running":
            return None
        if run_id is not None and self.swap["run_id"] != run_id:
            return None
        self.swap.update(fields)
        return dict(self.swap)

    async def complete_embedder_swap(self, partition: str, *, run_id: str) -> dict | None:
        if self.swap is None or self.swap["status"] != "running" or self.swap["run_id"] != run_id:
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
        self.on_searchable = lambda field: None

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

    async def make_searchable(self, field: str) -> None:
        self.on_searchable(field)


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


class FakeModelEndpointRepo:
    """The endpoints as the database has them — which a process's registry may not."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    async def get(self, name: str, model_type: str):
        row = self.rows.get(name)
        return SimpleNamespace(**row) if row is not None else None


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


def _make(
    files=None, chunks=None, *, repo=None, task_count=0, monkeypatch=None, stored_endpoints=None, resume_interval=60.0
):
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
    settings = _settings()
    endpoint_repo = FakeModelEndpointRepo(stored_endpoints or {})
    parts = {
        "repo": repo or FakePartitionRepo(),
        "documents": FakeDocumentRepo(files),
        "store": FakeVectorStore(chunks),
        "partitions": FakePartitionService(),
        "embedder": embedder,
        "settings": settings,
        "endpoints": endpoint_repo,
        "factory_calls": [],
        "built_from": [],
        "refreshes": 0,
    }

    def factory(name: str):
        parts["factory_calls"].append(name)
        # What the registry says when the embedder is built is what it embeds with.
        parts["built_from"].append(getattr(settings.models.embedder.get(name), "endpoint", None))
        return embedder

    async def refresh_endpoints() -> None:
        parts["refreshes"] += 1
        for name, row in endpoint_repo.rows.items():
            settings.models.embedder[name] = ModelEndpointConfig(**row)

    service = EmbedderSwapService(
        partition_repo=parts["repo"],
        document_repo=parts["documents"],
        vector_store=parts["store"],
        partition_service=parts["partitions"],
        config=settings,
        embedder_factory=factory,
        collection="vdb",
        model_endpoint_repo=endpoint_repo,
        refresh_endpoints=refresh_endpoints,
        task_state_manager_factory=lambda: object(),
        resume_interval=resume_interval,
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
    # The runner's, not the API's.
    assert "run_id" not in swap
    assert set(parts["factory_calls"]) == {"bge-m3"}
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


async def test_the_target_field_answers_searches_before_the_partition_switches(monkeypatch):
    """Milvus refuses searches on a newly added field until its data is flushed."""
    service, parts = _make(monkeypatch=monkeypatch)
    seen = []
    parts["store"].on_searchable = lambda field: seen.append((field, parts["repo"].swap["status"]))

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert seen == [(TARGET_FIELD, "running")]
    assert parts["repo"].swap["status"] == "completed"


async def test_a_target_field_that_never_answers_fails_the_swap(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)

    def refuse(field):
        raise VDBSearchError(f"`{field}` still refuses searches")

    parts["store"].on_searchable = refuse

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["repo"].swap["status"] == "failed"
    assert parts["repo"].rows["p1"]["embedder"] != "bge-m3"


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

    started = await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["embedder"].calls == [["hello"]]
    assert set(parts["documents"].recorded) == {"b"}
    # The progress counts the files to re-embed, not the ones skipped.
    assert started["files_total"] == 1
    assert parts["repo"].swap["files_done"] == parts["repo"].swap["files_total"] == 1


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


def _with_window(parts: dict, window: int) -> None:
    parts["settings"].models.embedder["bge-m3"] = ModelEndpointConfig(
        endpoint="http://bge:8000/v1", vector_field=TARGET_FIELD, extra={"max_model_len": window}
    )


async def test_chunks_longer_than_the_targets_window_are_counted(monkeypatch):
    """Indexing sizes chunks for the embedder it indexes with, and warns about
    the ones its window cuts. The swap embeds them as they are, so it says how
    many the target's window cuts."""
    chunks = {
        "a": [{"_id": 1, "text": "one", "token_count": 600}, {"_id": 2, "text": "three", "token_count": 100}],
        # At the window itself: one token over what vLLM is asked to keep.
        "b": [{"_id": 3, "text": "hello", "token_count": 512}, {"_id": 4, "text": "unmeasured"}],
    }
    service, parts = _make(chunks=chunks, monkeypatch=monkeypatch)
    _with_window(parts, 512)

    started = await service.start("p1", "bge-m3")
    await _finish(service)

    assert started["chunks_over_window"] == 0
    assert parts["repo"].swap["chunks_over_window"] == 2
    # Still embedded, as at indexing: the count is a warning, not a refusal.
    assert parts["store"].written[TARGET_FIELD].keys() == {"1", "2", "3", "4"}
    assert parts["repo"].swap["status"] == "completed"


async def test_the_window_is_the_servers_when_it_serves_less(monkeypatch):
    service, parts = _make(
        chunks={"a": [{"_id": 1, "text": "one", "token_count": 300}], "b": []}, monkeypatch=monkeypatch
    )
    _with_window(parts, 512)

    async def served_window() -> int:
        return 256

    parts["embedder"].served_window = served_window

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["repo"].swap["chunks_over_window"] == 1


async def test_a_resumed_swap_adds_to_the_chunks_it_already_counted(monkeypatch):
    files = [
        {"file_id": "a", "embedder_model_name": "BAAI/bge-m3", "embedder_vector_field": TARGET_FIELD},
        {"file_id": "b", "embedder_model_name": "intfloat/e5", "embedder_vector_field": SOURCE_FIELD},
    ]
    repo = FakePartitionRepo()
    service, parts = _make(
        files, {"b": [{"_id": 3, "text": "hello", "token_count": 900}]}, repo=repo, monkeypatch=monkeypatch
    )
    _with_window(parts, 512)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)
    repo.swap.update(files_done=1, chunks_over_window=4)

    await service.resume_running()
    await _finish(service)

    assert repo.swap["chunks_over_window"] == 5


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


async def test_a_swap_does_not_start_while_a_file_is_copied_in(monkeypatch):
    """A copy runs outside the fence and writes its file row last, so the job
    would not list it and it would stay in the old field."""
    repo = FakePartitionRepo()
    checked: list[str] = []

    async def copy_in_progress(name: str) -> bool:
        checked.append(name)
        return True

    repo.copy_in_progress = copy_in_progress
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)

    with pytest.raises(ConflictError, match="being copied") as exc:
        await service.start("p1", "bge-m3")

    assert exc.value.code == "INDEXING_IN_PROGRESS"
    assert checked == ["p1"]
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


async def test_a_target_edited_by_another_process_is_read_before_embedding(monkeypatch):
    """The registry is a cache: another API process can have edited the endpoint.

    Embedding with the endpoint as this process last saw it would fill the
    target field with vectors no query can match, under provenance naming the
    model it thought it called — and the partition is about to read that field.
    """
    edited = {
        "endpoint": "http://bge-2:8000/v1",
        "model_name": "BAAI/bge-m3",
        "extra": {},
        "vector_field": TARGET_FIELD,
    }
    service, parts = _make(monkeypatch=monkeypatch, stored_endpoints={"bge-m3": edited})

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["refreshes"] == 1
    # Both the admission that answered the request and the job that embedded.
    assert parts["built_from"] == ["http://bge-2:8000/v1", "http://bge-2:8000/v1"]
    assert parts["repo"].swap["status"] == "completed"


async def test_a_target_the_registry_already_has_right_is_not_reloaded(monkeypatch):
    """One row read per swap; a reload is for an edit this process missed."""
    unchanged = {"endpoint": "http://bge:8000/v1", "model_name": None, "extra": None, "vector_field": TARGET_FIELD}
    service, parts = _make(monkeypatch=monkeypatch, stored_endpoints={"bge-m3": unchanged})

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["refreshes"] == 0
    assert parts["repo"].swap["status"] == "completed"


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


async def test_a_runner_left_behind_by_a_newer_swap_neither_advances_nor_completes_it(monkeypatch):
    """Cancelled through another process, which cannot stop this job, then started
    again onto another embedder. The row is running again, but it is another run:
    were this runner to complete it, the partition would move to an embedder
    whose field nobody filled, and every file would vanish from its searches."""
    service, parts = _make(monkeypatch=monkeypatch)
    repo = parts["repo"]
    original_embed = parts["embedder"].embed

    async def embed_while_replaced(texts):
        if repo.swap["run_id"] == "run-1":
            repo.swap["status"] = "cancelled"
            await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="e5-v2", files_total=2)
        return await original_embed(texts)

    parts["embedder"].embed = embed_while_replaced

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert parts["embedder"].calls == [["one", "three"]]
    assert repo.swap["run_id"] == "run-2"
    assert repo.swap["status"] == "running"
    assert repo.swap["files_done"] == 0
    assert repo.rows["p1"]["embedder"] == "e5"
    assert repo.partition_updates == []


async def test_a_runner_left_behind_does_not_fail_the_newer_swap(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    repo = parts["repo"]

    async def fail_once_replaced(texts):
        repo.swap["status"] = "cancelled"
        await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="e5-v2", files_total=2)
        raise RuntimeError("vLLM unreachable")

    parts["embedder"].embed = fail_once_replaced

    await service.start("p1", "bge-m3")
    await _finish(service)

    assert repo.swap["run_id"] == "run-2"
    assert repo.swap["status"] == "running"
    assert repo.swap["error"] is None


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


async def test_a_resumed_swap_counts_on_from_the_files_it_already_did(monkeypatch):
    """File a was re-embedded before the restart: it is recorded with the target
    now, so it drops out of the files left, and stays counted as done."""
    files = [
        {"file_id": "a", "embedder_model_name": "BAAI/bge-m3", "embedder_vector_field": TARGET_FIELD},
        {"file_id": "b", "embedder_model_name": "intfloat/e5", "embedder_vector_field": SOURCE_FIELD},
    ]
    repo = FakePartitionRepo()
    service, parts = _make(files, repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)
    repo.swap["files_done"] = 1
    progress: list[dict] = []
    original_update = repo.update_embedder_swap

    async def record(partition, *, run_id=None, **fields):
        progress.append(fields)
        return await original_update(partition, run_id=run_id, **fields)

    repo.update_embedder_swap = record

    await service.resume_running()
    await _finish(service)

    assert parts["embedder"].calls == [["hello"]]
    assert progress == [{"files_total": 2, "files_done": 1}, {"files_done": 2, "chunks_over_window": 0}]
    assert repo.swap["status"] == "completed"


async def test_a_swap_another_process_runs_is_left_to_it(monkeypatch):
    repo = FakePartitionRepo(claimed=False)
    service, parts = _make(repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    await service.resume_running()
    await _finish(service)

    assert parts["embedder"].calls == []
    assert repo.swap["status"] == "running"


async def _shutdown(service: EmbedderSwapService) -> None:
    # Fails instead of hanging if the watcher is no longer cancelled.
    await asyncio.wait_for(service.shutdown(), timeout=5)


async def _until(condition, rounds: int = 200) -> None:
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0.005)


async def test_a_swap_another_process_lets_go_of_is_claimed_on_a_later_round(monkeypatch):
    """A rolling deploy: the old pod held the swap when this one started, then
    stopped its job as it shut down. Claimed once at startup, the swap would
    stay running with nothing re-embedding, and its uploads refused."""
    repo = FakePartitionRepo(claimed=False)
    service, parts = _make(repo=repo, monkeypatch=monkeypatch, resume_interval=0.005)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    assert await service.watch() == 1
    await asyncio.sleep(0.02)
    assert parts["embedder"].calls == []  # still the old pod's

    repo.claimed = True  # the old pod is gone
    await _until(lambda: repo.swap["status"] == "completed")

    assert repo.swap["status"] == "completed"
    await _shutdown(service)


async def test_a_failed_check_does_not_stop_the_watch(monkeypatch):
    repo = FakePartitionRepo()
    service, _ = _make(repo=repo, monkeypatch=monkeypatch, resume_interval=0.005)
    await service.watch()
    original_list = repo.list_embedder_swaps
    failures = []

    async def flaky(status=None):
        if not failures:
            failures.append(status)
            raise ConnectionError("postgres restarting")
        return await original_list(status)

    repo.list_embedder_swaps = flaky
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)
    await _until(lambda: repo.swap["status"] == "completed")

    assert failures == ["running"]
    assert repo.swap["status"] == "completed"
    await _shutdown(service)


async def test_shutdown_stops_the_watch_before_the_jobs(monkeypatch):
    """Or it could start a job again while the others stop."""
    service, _ = _make(monkeypatch=monkeypatch)
    await service.watch()
    watcher = service._watcher

    await _shutdown(service)

    assert watcher.cancelled()
    assert service._watcher is None


async def test_reading_a_running_swap_restarts_a_stranded_job(monkeypatch):
    """Its runner died: polling it is enough to get it going again."""
    repo = FakePartitionRepo()
    service, _ = _make(repo=repo, monkeypatch=monkeypatch)
    await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=2)

    await service.get("p1")
    await _finish(service)

    assert repo.swap["status"] == "completed"


async def test_a_partition_that_never_swapped_has_no_swap(monkeypatch):
    # Not an error: the admin UI asks on every page, and a 404 here was logged
    # at ERROR for each view of a partition that never swapped.
    service, _ = _make(monkeypatch=monkeypatch)

    assert await service.get("p1") is None


async def test_shutdown_stops_jobs_without_ending_their_swaps(monkeypatch):
    service, parts = _make(monkeypatch=monkeypatch)
    parts["embedder"].gate = asyncio.Event()
    await service.start("p1", "bge-m3")
    await asyncio.sleep(0)

    await _shutdown(service)

    assert parts["repo"].swap["status"] == "running"
