"""Unit tests for OpenRAGApplicationService.

Tests cover the methods that live directly in the service (not delegated to
IndexationService / SearchToolService which have their own test suites).

Ray and utils.dependencies are stubbed before any openrag import.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub utils.dependencies BEFORE importing anything from openrag.
# We use setdefault so this file does not override a stub already installed by
# another test that runs earlier in the session.
# We do NOT stub `ray` itself here — service.py only references ray inside
# method bodies (never at module-load time), so no module-level ray stub is
# needed.  Individual tests that call ray.cancel / ray.kill / ray.get_actor
# use monkeypatch to patch the `ray` module at runtime.
# We do NOT stub components.mcp.adapters here because test_ray_indexer_adapter
# imports the real RayIndexerSearchGateway from that package; we instead patch
# the gateway inside the svc fixture.
# ---------------------------------------------------------------------------

_stub_deps = ModuleType("utils.dependencies")
_stub_deps.get_vectordb = MagicMock()
_stub_deps.get_task_state_manager = MagicMock(return_value=MagicMock())
_stub_deps.get_indexer = MagicMock()
sys.modules.setdefault("utils.dependencies", _stub_deps)

from components.app.service import OpenRAGApplicationService  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------


class _RemoteCall:
    """Wraps a coroutine so it can be awaited via `.remote()`."""

    def __init__(self, coro_fn):
        self._fn = coro_fn

    async def remote(self, *args, **kwargs):
        return await self._fn(*args, **kwargs)


class _SyncRemoteCall:
    """Wraps a sync callable so it can be awaited via `.remote()`."""

    def __init__(self, fn):
        self._fn = fn

    async def remote(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


def _make_chunk(text: str, chunk_id: str = "c1", partition: str = "p1", **extra):
    return SimpleNamespace(
        page_content=text,
        metadata={"_id": chunk_id, "partition": partition, **extra},
    )


class _FakeVectorDB:
    """Minimal fake of the MilvusDB Ray actor surface."""

    def __init__(self):
        self._users: list[dict] = []
        self._partitions: list[dict] = []
        self._chunks: dict[str, SimpleNamespace] = {}
        self._all_chunks: list = []
        self._members: list[dict] = []
        self._created_user: dict = {}
        self._token_user: dict = {}

        self.list_users = _SyncRemoteCall(lambda: self._users)
        self.list_partitions = _SyncRemoteCall(lambda: self._partitions)
        self.get_chunk_by_id = _SyncRemoteCall(lambda cid: self._chunks.get(cid))
        self.list_all_chunk = _SyncRemoteCall(lambda partition, include_embedding=True: self._all_chunks)
        self.list_partition_members = _SyncRemoteCall(lambda partition: self._members)
        self.create_partition = _SyncRemoteCall(lambda partition, user_id: None)
        self.delete_partition = _SyncRemoteCall(lambda partition: None)
        self.add_partition_member = _SyncRemoteCall(lambda partition, user_id, role: None)
        self.remove_partition_member = _SyncRemoteCall(lambda partition, user_id: None)
        self.update_partition_member_role = _SyncRemoteCall(lambda partition, user_id, new_role: None)
        self.create_user = _SyncRemoteCall(
            lambda display_name=None, external_user_id=None, is_admin=False: self._created_user
        )
        self.get_user = _SyncRemoteCall(lambda user_id: {"id": user_id, "display_name": "test"})
        self.delete_user = _SyncRemoteCall(lambda user_id: None)
        self.regenerate_user_token = _SyncRemoteCall(lambda user_id: {"id": user_id, "token": "new-token"})


class _FakeTaskStateManager:
    def __init__(self):
        self._states: dict = {}
        self._errors: dict = {}
        self._refs: dict = {}
        self._all_states: dict = {}
        self._pool_info: dict = {
            "total_capacity": 10,
            "pool_size": 2,
            "max_tasks_per_worker": 5,
        }

        async def _set_state(task_id, state):
            self._states[task_id] = state

        async def _set_ref(task_id, ref):
            self._refs[task_id] = ref

        self.set_state = SimpleNamespace(remote=_set_state)
        self.set_object_ref = SimpleNamespace(remote=_set_ref)
        self.get_error = _SyncRemoteCall(lambda task_id: self._errors.get(task_id))
        self.get_object_ref = _SyncRemoteCall(lambda task_id: self._refs.get(task_id))
        self.get_all_states = _SyncRemoteCall(lambda: self._all_states)
        self.get_pool_info = _SyncRemoteCall(lambda: self._pool_info)


class _FakeTask:
    """Minimal fake for a Ray ObjectRef returned by indexer.add_file.remote(...)."""

    def __init__(self, task_id: str):
        self._task_id = task_id

    def task_id(self):
        return SimpleNamespace(hex=lambda: self._task_id)


class _FakeIndexer:
    """Minimal fake for the Indexer Ray actor."""

    def __init__(self, task_id: str = "abc123"):
        self._task_id = task_id
        self._deleted: list[tuple] = []

        # Ray's .remote() calls are *synchronous* — they return an ObjectRef
        # immediately (no await).  The fake must match that behaviour.
        task_id_str = task_id

        def _add_file(path, metadata, partition, user=None):
            return _FakeTask(task_id_str)

        async def _delete_file_async(file_id, partition):
            self._deleted.append((file_id, partition))

        class _DeleteRemote:
            def __init__(self_, parent):
                self_._parent = parent

            async def remote(self_, file_id, partition):
                self_._parent._deleted.append((file_id, partition))

        class _AddRemote:
            def remote(self_, *args, **kwargs):
                return _add_file(*args, **kwargs)

        self.add_file = _AddRemote()
        self.delete_file = _DeleteRemote(self)


# ---------------------------------------------------------------------------
# Service fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc(monkeypatch):
    """Return an OpenRAGApplicationService.

    Patches RayIndexerSearchGateway with a no-op stub so Ray is never
    contacted during construction.
    """
    import components.mcp.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "RayIndexerSearchGateway", MagicMock)
    return OpenRAGApplicationService()


# ===========================================================================
# get_supported_types
# ===========================================================================


@pytest.mark.asyncio
async def test_get_supported_types(svc):
    result = await svc.get_supported_types(
        accepted_formats={"pdf", "docx"},
        dict_mimetypes={"application/pdf": "pdf"},
    )
    assert set(result["extensions"]) == {"pdf", "docx"}
    assert "application/pdf" in result["mimetypes"]


# ===========================================================================
# add_file
# ===========================================================================


@pytest.mark.asyncio
async def test_add_file_queues_task(svc, tmp_path):
    indexer = _FakeIndexer(task_id="task001")
    tsm = _FakeTaskStateManager()
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"content")

    result = await svc.add_file(
        file_path=file_path,
        metadata={"file_id": "f1"},
        partition="p1",
        user={"id": 1},
        indexer=indexer,
        task_state_manager=tsm,
    )

    assert result["task_id"] == "task001"
    assert tsm._states["task001"] == "QUEUED"
    assert "ref" in tsm._refs["task001"]


# ===========================================================================
# replace_file
# ===========================================================================


@pytest.mark.asyncio
async def test_replace_file_deletes_then_queues(svc, tmp_path):
    indexer = _FakeIndexer(task_id="task002")
    tsm = _FakeTaskStateManager()
    file_path = tmp_path / "new.pdf"
    file_path.write_bytes(b"new content")

    result = await svc.replace_file(
        file_id="old_file",
        file_path=file_path,
        metadata={"file_id": "old_file"},
        partition="p1",
        user={"id": 1},
        indexer=indexer,
        task_state_manager=tsm,
    )

    assert result["task_id"] == "task002"
    assert ("old_file", "p1") in indexer._deleted
    assert tsm._states["task002"] == "QUEUED"


# ===========================================================================
# get_task_error
# ===========================================================================


@pytest.mark.asyncio
async def test_get_task_error_returns_lines(svc):
    tsm = _FakeTaskStateManager()
    tsm._errors["t1"] = "line1\nline2\nline3"

    result = await svc.get_task_error(task_state_manager=tsm, task_id="t1")

    assert result["task_id"] == "t1"
    assert result["traceback"] == ["line1", "line2", "line3"]


@pytest.mark.asyncio
async def test_get_task_error_no_error_returns_empty(svc):
    tsm = _FakeTaskStateManager()

    result = await svc.get_task_error(task_state_manager=tsm, task_id="missing")
    assert result["traceback"] == []


# ===========================================================================
# cancel_task
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_task_calls_ray_cancel(svc, monkeypatch):
    import ray as ray_mod

    cancelled = {}

    def _cancel(ref, recursive=False):
        cancelled["ref"] = ref
        cancelled["recursive"] = recursive

    monkeypatch.setattr(ray_mod, "cancel", _cancel)

    tsm = _FakeTaskStateManager()
    fake_ref = object()
    tsm._refs["t2"] = {"ref": fake_ref}

    result = await svc.cancel_task(task_state_manager=tsm, task_id="t2")

    assert "Cancellation signal sent" in result["message"]
    assert cancelled["ref"] is fake_ref
    assert cancelled["recursive"] is True


# ===========================================================================
# get_extract
# ===========================================================================


@pytest.mark.asyncio
async def test_get_extract_returns_chunk(svc):
    vdb = _FakeVectorDB()
    chunk = _make_chunk("hello world", chunk_id="cx1")
    vdb._chunks["cx1"] = chunk

    result = await svc.get_extract(vectordb=vdb, extract_id="cx1")

    assert result["page_content"] == "hello world"
    assert result["metadata"]["_id"] == "cx1"


@pytest.mark.asyncio
async def test_get_extract_missing_returns_empty(svc):
    vdb = _FakeVectorDB()
    result = await svc.get_extract(vectordb=vdb, extract_id="missing")
    assert result == {}


# ===========================================================================
# list_partition_chunks
# ===========================================================================


@pytest.mark.asyncio
async def test_list_partition_chunks(svc):
    vdb = _FakeVectorDB()
    vdb._all_chunks = [_make_chunk("c1"), _make_chunk("c2")]

    result = await svc.list_partition_chunks(vectordb=vdb, partition="p1")
    assert len(result["chunks"]) == 2


# ===========================================================================
# create_partition / delete_partition
# ===========================================================================


@pytest.mark.asyncio
async def test_create_partition(svc):
    vdb = _FakeVectorDB()
    result = await svc.create_partition(vectordb=vdb, partition="new_part", user_id=1)
    assert result["created"] is True
    assert result["partition"] == "new_part"


@pytest.mark.asyncio
async def test_delete_partition(svc):
    vdb = _FakeVectorDB()
    result = await svc.delete_partition(vectordb=vdb, partition="old_part")
    assert result["deleted"] is True


# ===========================================================================
# Partition membership
# ===========================================================================


@pytest.mark.asyncio
async def test_list_partition_users(svc):
    vdb = _FakeVectorDB()
    vdb._members = [{"user_id": 1, "role": "owner"}]
    result = await svc.list_partition_users(vectordb=vdb, partition="p1")
    assert len(result["members"]) == 1


@pytest.mark.asyncio
async def test_add_partition_user(svc):
    vdb = _FakeVectorDB()
    result = await svc.add_partition_user(vectordb=vdb, partition="p1", user_id=2, role="viewer")
    assert result["added"] is True


@pytest.mark.asyncio
async def test_remove_partition_user(svc):
    vdb = _FakeVectorDB()
    result = await svc.remove_partition_user(vectordb=vdb, partition="p1", user_id=2)
    assert result["removed"] is True


@pytest.mark.asyncio
async def test_update_partition_user_role(svc):
    vdb = _FakeVectorDB()
    result = await svc.update_partition_user_role(vectordb=vdb, partition="p1", user_id=2, role="editor")
    assert result["updated"] is True


# ===========================================================================
# get_queue_info
# ===========================================================================


@pytest.mark.asyncio
async def test_get_queue_info_structure(svc):
    tsm = _FakeTaskStateManager()
    tsm._all_states = {
        "t1": "QUEUED",
        "t2": "COMPLETED",
        "t3": "FAILED",
        "t4": "INSERTING",
    }

    result = await svc.get_queue_info(task_state_manager=tsm)

    assert result["tasks"]["active"] == 2  # QUEUED + INSERTING
    assert result["tasks"]["total_completed"] == 1
    assert result["tasks"]["total_failed"] == 1
    assert result["workers"]["total_slots"] == 10


# ===========================================================================
# Users
# ===========================================================================


@pytest.mark.asyncio
async def test_list_users(svc):
    vdb = _FakeVectorDB()
    vdb._users = [{"id": 1}, {"id": 2}]
    result = await svc.list_users(vectordb=vdb)
    assert len(result["users"]) == 2


@pytest.mark.asyncio
async def test_get_current_user_returns_user(svc):
    user = {"id": 5, "display_name": "Alice"}
    result = await svc.get_current_user(user=user)
    assert result == user


@pytest.mark.asyncio
async def test_create_user(svc):
    vdb = _FakeVectorDB()
    vdb._created_user = {"id": 10, "display_name": "Bob", "token": "tok"}
    result = await svc.create_user(vectordb=vdb, display_name="Bob", is_admin=False)
    assert result["id"] == 10


@pytest.mark.asyncio
async def test_get_user(svc):
    vdb = _FakeVectorDB()
    result = await svc.get_user(vectordb=vdb, user_id=7)
    assert result["id"] == 7


@pytest.mark.asyncio
async def test_delete_user_account(svc):
    vdb = _FakeVectorDB()
    result = await svc.delete_user_account(vectordb=vdb, user_id=3)
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_regenerate_user_token(svc):
    vdb = _FakeVectorDB()
    result = await svc.regenerate_user_token(vectordb=vdb, user_id=4)
    assert result["token"] == "new-token"


# ===========================================================================
# list_ray_actors
# ===========================================================================


@pytest.mark.asyncio
async def test_list_ray_actors(svc):
    actors = [{"actor_id": "a1", "name": "Indexer", "state": "ALIVE"}]
    result = await svc.list_ray_actors(actors=actors)
    assert result["actors"] == actors


# ===========================================================================
# restart_actor
# ===========================================================================


@pytest.mark.asyncio
async def test_restart_actor_unknown_raises(svc):
    with pytest.raises(KeyError, match="Unknown actor"):
        await svc.restart_actor(actor_name="Ghost", actor_creation_map={})


@pytest.mark.asyncio
async def test_restart_actor_kills_and_recreates(svc, monkeypatch):
    import ray as ray_mod

    killed = {}
    fake_actor_handle = SimpleNamespace(_actor_id=SimpleNamespace(hex=lambda: "new-id-hex"))

    monkeypatch.setattr(ray_mod, "get_actor", lambda name, namespace=None: SimpleNamespace())
    monkeypatch.setattr(ray_mod, "kill", lambda actor, no_restart=False: killed.update({"done": True}))

    creation_map = {
        "Indexer": lambda: fake_actor_handle,
    }

    result = await svc.restart_actor(actor_name="Indexer", actor_creation_map=creation_map)

    assert result["actor_name"] == "Indexer"
    assert result["actor_id"] == "new-id-hex"
    assert killed["done"] is True


@pytest.mark.asyncio
async def test_restart_actor_handles_missing_actor(svc, monkeypatch):
    """When ray.get_actor raises ValueError (actor not found), creation still proceeds."""
    import ray as ray_mod

    fake_actor_handle = SimpleNamespace(_actor_id=SimpleNamespace(hex=lambda: "fresh-id"))

    monkeypatch.setattr(
        ray_mod, "get_actor", lambda name, namespace=None: (_ for _ in ()).throw(ValueError("not found"))
    )

    creation_map = {
        "Indexer": lambda: fake_actor_handle,
    }

    result = await svc.restart_actor(actor_name="Indexer", actor_creation_map=creation_map)
    assert result["actor_id"] == "fresh-id"


# ===========================================================================
# list_tools
# ===========================================================================


@pytest.mark.asyncio
async def test_list_tools(svc):
    tools = [SimpleNamespace(name="extractText", description="Extract text")]
    result = await svc.list_tools(tools=tools)
    assert result["tools"] == tools


# ===========================================================================
# list_models
# ===========================================================================


@pytest.mark.asyncio
async def test_list_models_builds_model_list(svc, monkeypatch):
    import sys
    import types

    # Stub 'consts' module
    fake_consts = types.ModuleType("consts")
    fake_consts.PARTITION_PREFIX = "openrag-"
    monkeypatch.setitem(sys.modules, "consts", fake_consts)

    vdb = _FakeVectorDB()
    # user_partitions already contains real partitions (not ["all"])
    user_partitions = [
        {"partition": "alpha", "created_at": 100},
        {"partition": "beta", "created_at": 200},
    ]

    result = await svc.list_models(vectordb=vdb, user_partitions=user_partitions)

    ids = [m["id"] for m in result["data"]]
    assert "openrag-alpha" in ids
    assert "openrag-beta" in ids
    assert "openrag-all" in ids
    assert result["object"] == "list"


@pytest.mark.asyncio
async def test_list_models_expands_all_partitions(svc, monkeypatch):
    import sys
    import types

    fake_consts = types.ModuleType("consts")
    fake_consts.PARTITION_PREFIX = "openrag-"
    monkeypatch.setitem(sys.modules, "consts", fake_consts)

    vdb = _FakeVectorDB()
    vdb._partitions = [
        {"partition": "x", "created_at": 1},
        {"partition": "y", "created_at": 2},
    ]
    # When user_partitions == [{"partition": "all"}], it expands via vectordb
    user_partitions = [{"partition": "all"}]

    result = await svc.list_models(vectordb=vdb, user_partitions=user_partitions)

    ids = [m["id"] for m in result["data"]]
    assert "openrag-x" in ids
    assert "openrag-y" in ids
