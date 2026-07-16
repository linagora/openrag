"""Unit tests for :class:`PartitionService` (Phase 8B.1)."""

from __future__ import annotations

import pytest
from core.utils.exceptions import NotFoundError, PartitionNotFoundError, UserNotFoundError, ValidationError
from services.orchestrators.partition_service import PartitionService


class FakePartitionRepo:
    def __init__(self, existing: set[str] | None = None, *, owned_count: int = 0):
        self._existing = existing if existing is not None else set()
        self._owned_count = owned_count
        self.created: list[tuple[str, int]] = []
        self.deleted: list[str] = []

    async def partition_exists(self, name: str) -> bool:
        return name in self._existing

    async def list_partitions(self) -> list[dict]:
        return [{"partition": p} for p in sorted(self._existing)]

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        if max_owned is not None and max_owned >= 0 and self._owned_count >= max_owned:
            raise ValidationError(
                f"Partition limit reached ({max_owned}). Contact an administrator.",
                status_code=403,
                code="PARTITION_LIMIT_EXCEEDED",
            )
        self._existing.add(name)
        self.created.append((name, user_id))
        return {"partition": name}

    async def delete_partition(self, name: str) -> bool:
        self.deleted.append(name)
        self._existing.discard(name)
        return True


class FakeMembershipRepo:
    def __init__(
        self,
        members: set[tuple[int, str]] | None = None,
        owned: dict[int, int] | None = None,
    ):
        self._members = members or set()
        self._owned = owned or {}  # user_id -> number of partitions owned
        self.added: list[tuple[str, int, str]] = []
        self.removed: list[tuple[str, int]] = []
        self.role_updates: list[tuple[str, int, str]] = []

    async def list_user_partitions(self, user_id: int):
        from core.models.user import PartitionRole, UserPartition

        return [
            UserPartition(user_id=user_id, partition=f"owned-{i}", role=PartitionRole.OWNER)
            for i in range(self._owned.get(user_id, 0))
        ]

    async def user_is_partition_member(self, user_id: int, partition: str) -> bool:
        return (user_id, partition) in self._members

    async def list_partition_members(self, partition: str) -> list[dict]:
        return [{"user_id": u, "role": "viewer"} for (u, p) in self._members if p == partition]

    async def add_partition_member(self, partition: str, user_id: int, role: str) -> bool:
        self.added.append((partition, user_id, role))
        return True

    async def remove_partition_member(self, partition: str, user_id: int) -> bool:
        self.removed.append((partition, user_id))
        return True

    async def update_partition_member_role(self, partition: str, user_id: int, new_role: str) -> bool:
        self.role_updates.append((partition, user_id, new_role))
        return True


class FakeDocumentRepo:
    def __init__(self, files: set[tuple[str, str]] | None = None, listing: dict | None = None):
        self._files = files or set()
        self._listing = listing if listing is not None else {}

    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool:
        return (file_id, partition) in self._files

    async def list_partition_files(self, partition: str, limit=None) -> dict:
        return self._listing

    async def get_files_by_relationship(self, partition: str, relationship_id: str) -> list[dict]:
        return [{"file_id": "a", "relationship_id": relationship_id}]

    async def get_file_ancestors(self, partition: str, file_id: str, max_ancestor_depth=None) -> list[dict]:
        return [{"file_id": "root"}, {"file_id": file_id}]


class FakeVectorStore:
    def __init__(self, ids=None, rows=None, exists=True):
        self._ids = ids or []
        self._rows = rows or []
        self._exists = exists
        self.deleted_ids: list[str] = []
        self.deleted_filters: list[dict] = []
        self.last_chunk_filters: dict | None = None

    async def collection_exists(self, name) -> bool:
        return self._exists

    async def query_ids_by_filter(self, collection, filters):
        return list(self._ids)

    async def delete(self, ids, collection="default") -> int:
        self.deleted_ids.extend(ids)
        return len(ids)

    async def delete_by_filter(self, filters) -> int:
        self.deleted_filters.append(dict(filters))
        before = len(self._rows)
        self._rows = [row for row in self._rows if not all(row.get(key) == value for key, value in filters.items())]
        return len(self._ids) or before - len(self._rows)

    async def query_chunks_by_filter(self, collection, filters, output_fields=None):
        self.last_chunk_filters = dict(filters)
        return [row for row in self._rows if all(row.get(key) == value for key, value in filters.items())]


class FakeUserRepo:
    def __init__(self, existing: set[int] | None = None):
        self._existing = existing if existing is not None else set()

    async def user_exists(self, user_id: int) -> bool:
        return user_id in self._existing


def _svc(
    *,
    prepo=None,
    mrepo=None,
    drepo=None,
    vstore=None,
    urepo=None,
    collection="vdb",
    tsm=None,
    task_cancel_timeout=60.0,
) -> PartitionService:
    return PartitionService(
        partition_repo=prepo or FakePartitionRepo(),
        membership_repo=mrepo or FakeMembershipRepo(),
        document_repo=drepo or FakeDocumentRepo(),
        vector_store=vstore or FakeVectorStore(),
        user_repo=urepo or FakeUserRepo(),
        collection=collection,
        task_state_manager=tsm,
        task_cancel_timeout=task_cancel_timeout,
    )


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_partition_conflict_raises_409():
    prepo = FakePartitionRepo(existing={"p1"})
    with pytest.raises(ValidationError) as ei:
        await _svc(prepo=prepo).create_partition("p1", 1)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_create_partition_enforces_owned_cap():
    prepo = FakePartitionRepo(existing=set(), owned_count=2)
    mrepo = FakeMembershipRepo(owned={7: 2})
    with pytest.raises(ValidationError) as exc:
        await _svc(prepo=prepo, mrepo=mrepo).create_partition("new", 7, max_owned=2)
    assert exc.value.status_code == 403
    assert exc.value.code == "PARTITION_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_create_partition_zero_cap_blocks_regular_user():
    prepo = FakePartitionRepo(existing=set(), owned_count=0)
    mrepo = FakeMembershipRepo(owned={7: 0})
    with pytest.raises(ValidationError) as exc:
        await _svc(prepo=prepo, mrepo=mrepo).create_partition("new", 7, max_owned=0)
    assert exc.value.status_code == 403
    assert prepo.created == []


@pytest.mark.asyncio
async def test_create_partition_negative_cap_disables_limit():
    prepo = FakePartitionRepo(existing=set(), owned_count=999)
    mrepo = FakeMembershipRepo(owned={7: 999})
    await _svc(prepo=prepo, mrepo=mrepo).create_partition("new", 7, max_owned=-1)
    assert prepo.created == [("new", 7)]


@pytest.mark.asyncio
async def test_create_partition_under_cap_succeeds():
    prepo = FakePartitionRepo(existing=set(), owned_count=1)
    mrepo = FakeMembershipRepo(owned={7: 1})
    await _svc(prepo=prepo, mrepo=mrepo).create_partition("new", 7, max_owned=5)
    assert "new" in prepo._existing


@pytest.mark.asyncio
async def test_create_partition_admin_bypass_cap_when_max_owned_none():
    prepo = FakePartitionRepo(existing=set(), owned_count=999)
    mrepo = FakeMembershipRepo(owned={7: 999})
    # max_owned=None (admin) → cap skipped even with many owned partitions.
    await _svc(prepo=prepo, mrepo=mrepo).create_partition("new", 7, max_owned=None)
    assert "new" in prepo._existing


@pytest.mark.asyncio
async def test_create_partition_success():
    prepo = FakePartitionRepo()
    await _svc(prepo=prepo).create_partition("new", 7)
    assert prepo.created == [("new", 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["all", "ALL", "All", "  all  "])
async def test_create_partition_rejects_reserved_name(name):
    # ``all`` is the cross-partition sentinel — a real partition with that name
    # would leak every partition to its owner. Rejected (400) before the row is
    # written, case-insensitively and after trimming, even when it doesn't exist.
    prepo = FakePartitionRepo()
    with pytest.raises(ValidationError) as ei:
        await _svc(prepo=prepo).create_partition(name, 1)
    assert ei.value.status_code == 400
    assert ei.value.code == "RESERVED_PARTITION_NAME"
    assert prepo.created == []


@pytest.mark.asyncio
async def test_delete_partition_missing_raises_404():
    with pytest.raises(PartitionNotFoundError):
        await _svc(prepo=FakePartitionRepo(existing=set())).delete_partition("ghost")


@pytest.mark.asyncio
async def test_delete_partition_deletes_vectors_before_rows():
    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore(ids=["c1", "c2"])
    call_order = []

    prepo_delete_orig = prepo.delete_partition

    async def tracked_prepo_delete(*args, **kwargs):
        call_order.append("prepo_delete")
        return await prepo_delete_orig(*args, **kwargs)

    prepo.delete_partition = tracked_prepo_delete
    vstore_delete_orig = vstore.delete_by_filter

    async def tracked_vstore_delete(*args, **kwargs):
        call_order.append("vstore_delete_by_filter")
        return await vstore_delete_orig(*args, **kwargs)

    vstore.delete_by_filter = tracked_vstore_delete

    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")

    assert call_order == ["vstore_delete_by_filter", "prepo_delete", "vstore_delete_by_filter"]
    assert vstore.deleted_filters == [{"partition": "p1"}, {"partition": "p1"}]
    assert vstore.deleted_ids == []
    assert prepo.deleted == ["p1"]


@pytest.mark.asyncio
async def test_delete_partition_no_vectors_still_deletes_rows():
    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore(ids=[])
    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")
    assert vstore.deleted_filters == [{"partition": "p1"}, {"partition": "p1"}]
    assert vstore.deleted_ids == []
    assert prepo.deleted == ["p1"]


@pytest.mark.asyncio
async def test_delete_partition_cleans_vectors_before_partition_name_reuse():
    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore(
        rows=[
            {"partition": "p1", "file_id": "old-file", "text": "old tenant data"},
            {"partition": "other", "file_id": "keep-file", "text": "other data"},
        ]
    )

    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")
    await _svc(prepo=prepo, vstore=vstore).create_partition("p1", user_id=42)

    assert await vstore.query_chunks_by_filter("vdb", {"partition": "p1"}) == []
    assert await vstore.query_chunks_by_filter("vdb", {"partition": "other"}) == [
        {"partition": "other", "file_id": "keep-file", "text": "other data"}
    ]


@pytest.mark.asyncio
async def test_delete_partition_skips_vectors_when_collection_absent():
    """Regression for #505: on a fresh stack nothing has ever been indexed, so
    the shared Milvus collection doesn't exist. Deleting a partition must still
    drop the relational rows without querying (and 500-ing on) the missing
    collection."""
    prepo = FakePartitionRepo(existing={"p1"})

    class ExplodingVectorStore(FakeVectorStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.delete_called = False

        async def query_ids_by_filter(self, collection, filters):
            raise AssertionError("must not query a collection that doesn't exist")

        async def delete(self, ids, collection="default") -> int:
            self.delete_called = True
            raise AssertionError("must not delete vectors when collection doesn't exist")

        async def delete_by_filter(self, filters) -> int:
            self.delete_called = True
            raise AssertionError("must not delete vectors when collection doesn't exist")

    vstore = ExplodingVectorStore(exists=False)
    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")
    assert prepo.deleted == ["p1"]
    assert vstore.deleted_ids == []
    assert vstore.delete_called is False


@pytest.mark.asyncio
async def test_delete_partition_cancels_active_indexing_tasks_before_cleanup():
    from unittest.mock import AsyncMock, MagicMock, patch

    prepo = FakePartitionRepo(existing={"p1"})
    ref = object()
    tsm = MagicMock()
    tsm.get_matching_active_task_refs = MagicMock()
    tsm.get_matching_active_task_refs.remote = AsyncMock(return_value={"task-1": {"ref": ref}})
    tsm.set_state = MagicMock()
    tsm.set_state.remote = AsyncMock(return_value=None)

    with patch("ray.cancel") as cancel:
        await _svc(prepo=prepo, tsm=tsm).delete_partition("p1")

    tsm.get_matching_active_task_refs.remote.assert_called_once_with(partition="p1", file_id=None)
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")


@pytest.mark.asyncio
async def test_delete_partition_does_not_cleanup_when_matching_task_has_no_ref():
    from unittest.mock import AsyncMock, MagicMock, patch

    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore()
    tsm = MagicMock()
    tsm.get_matching_active_task_refs = MagicMock()
    tsm.get_matching_active_task_refs.remote = AsyncMock(return_value={"task-1": {"ref": None}})
    tsm.set_state = MagicMock()
    tsm.set_state.remote = AsyncMock(return_value=None)

    with pytest.raises(TimeoutError, match="become cancellable"), patch("ray.cancel") as cancel:
        await _svc(prepo=prepo, vstore=vstore, tsm=tsm, task_cancel_timeout=0.01).delete_partition("p1")

    cancel.assert_not_called()
    tsm.set_state.remote.assert_not_called()
    assert vstore.deleted_filters == []
    assert prepo.deleted == []


@pytest.mark.asyncio
async def test_delete_partition_keeps_rows_if_vector_store_delete_fails():
    prepo = FakePartitionRepo(existing={"p1"})

    class FailingVectorStore(FakeVectorStore):
        async def delete_by_filter(self, filters) -> int:
            raise Exception("Milvus connection failed")

    vstore = FailingVectorStore(ids=["c1", "c2"])

    with pytest.raises(Exception, match="Milvus connection failed"):
        await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")

    assert prepo.deleted == []


@pytest.mark.asyncio
async def test_delete_partition_keeps_success_when_post_delete_cleanup_fails():
    prepo = FakePartitionRepo(existing={"p1"})

    class FailingSecondSweepVectorStore(FakeVectorStore):
        async def delete_by_filter(self, filters) -> int:
            self.deleted_filters.append(dict(filters))
            if len(self.deleted_filters) == 2:
                raise Exception("Milvus connection failed")
            return 1

    vstore = FailingSecondSweepVectorStore()

    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")

    assert prepo.deleted == ["p1"]
    assert vstore.deleted_filters == [{"partition": "p1"}, {"partition": "p1"}]


# --------------------------------------------------------------------------- #
# file / chunk reads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_files_missing_partition_404():
    with pytest.raises(PartitionNotFoundError):
        await _svc(prepo=FakePartitionRepo(set())).list_files("nope")


@pytest.mark.asyncio
async def test_list_files_empty_listing_returns_empty_list():
    svc = _svc(prepo=FakePartitionRepo({"p"}), drepo=FakeDocumentRepo(listing={}))
    assert await svc.list_files("p") == []


@pytest.mark.asyncio
async def test_get_file_chunks_missing_file_404():
    svc = _svc(drepo=FakeDocumentRepo(files=set()))
    with pytest.raises(NotFoundError) as ei:
        await svc.get_file_chunks("p", "f")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_file_chunks_strips_text_keeps_id_and_caps_limit():
    rows = [{"_id": str(i), "text": "body", "page": i, "partition": "p", "file_id": "f"} for i in range(5)]
    svc = _svc(
        drepo=FakeDocumentRepo(files={("f", "p")}),
        vstore=FakeVectorStore(rows=rows),
    )
    out = await svc.get_file_chunks("p", "f", limit=3)
    assert len(out) == 3
    assert all("text" not in r for r in out)
    assert all("_id" in r for r in out)


@pytest.mark.asyncio
async def test_list_all_chunks_excludes_vector_when_no_embedding():
    rows = [{"text": "t", "_id": "1", "partition": "p", "vector": [0.1, 0.2]}]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    out = await svc.list_all_chunks("p", include_embedding=False)
    assert out[0]["content"] == "t"
    assert "vector" not in out[0]["metadata"]
    assert "text" not in out[0]["metadata"]


@pytest.mark.asyncio
async def test_list_all_chunks_stringifies_vector_when_included():
    rows = [{"text": "t", "_id": "1", "partition": "p", "vector": [0.1, 0.2]}]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    out = await svc.list_all_chunks("p", include_embedding=True)
    assert isinstance(out[0]["metadata"]["vector"], str)


async def test_list_all_chunks_without_file_id_filters_partition_only():
    vstore = FakeVectorStore(rows=[{"text": "t", "_id": "1", "partition": "p"}])
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=vstore)
    await svc.list_all_chunks("p", include_embedding=False)
    assert vstore.last_chunk_filters == {"partition": "p"}


@pytest.mark.asyncio
async def test_list_all_chunks_scopes_to_file_id_when_given():
    """file_id is pushed down to the vector store so the detail view is O(file)."""
    vstore = FakeVectorStore(rows=[{"text": "t", "_id": "1", "partition": "p", "file_id": "f-123"}])
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=vstore)
    await svc.list_all_chunks("p", include_embedding=False, file_id="f-123")
    assert vstore.last_chunk_filters == {"partition": "p", "file_id": "f-123"}


async def test_list_all_chunks_applies_limit():
    rows = [{"text": str(i), "_id": str(i), "partition": "p"} for i in range(5)]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    out = await svc.list_all_chunks("p", include_embedding=False, limit=2)
    assert len(out) == 2


async def test_list_all_chunks_rejects_negative_limit():
    rows = [{"text": str(i), "_id": str(i), "partition": "p"} for i in range(5)]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    with pytest.raises(ValidationError) as ei:
        await svc.list_all_chunks("p", include_embedding=False, limit=-1)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_get_file_chunks_rejects_negative_limit():
    rows = [{"_id": str(i), "text": "body", "partition": "p", "file_id": "f"} for i in range(5)]
    svc = _svc(
        drepo=FakeDocumentRepo(files={("f", "p")}),
        vstore=FakeVectorStore(rows=rows),
    )
    with pytest.raises(ValidationError) as ei:
        await svc.get_file_chunks("p", "f", limit=-1)
    assert ei.value.status_code == 422


# --------------------------------------------------------------------------- #
# membership
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_members_missing_partition_404():
    with pytest.raises(PartitionNotFoundError):
        await _svc(prepo=FakePartitionRepo(set())).list_members("x")


@pytest.mark.asyncio
async def test_add_member_checks_partition_and_user():
    mrepo = FakeMembershipRepo()
    svc = _svc(
        prepo=FakePartitionRepo({"p"}),
        mrepo=mrepo,
        urepo=FakeUserRepo({9}),
    )
    await svc.add_member("p", 9, "editor")
    assert mrepo.added == [("p", 9, "editor")]


@pytest.mark.asyncio
async def test_add_member_unknown_user_404():
    svc = _svc(prepo=FakePartitionRepo({"p"}), urepo=FakeUserRepo(set()))
    with pytest.raises(UserNotFoundError):
        await svc.add_member("p", 123, "viewer")


@pytest.mark.asyncio
async def test_remove_member_requires_existing_membership():
    svc = _svc(
        prepo=FakePartitionRepo({"p"}),
        mrepo=FakeMembershipRepo(members=set()),
        urepo=FakeUserRepo({9}),
    )
    with pytest.raises(NotFoundError) as ei:
        await svc.remove_member("p", 9)
    assert ei.value.code == "MEMBERSHIP_NOT_FOUND"


@pytest.mark.asyncio
async def test_update_role_success():
    mrepo = FakeMembershipRepo(members={(9, "p")})
    svc = _svc(prepo=FakePartitionRepo({"p"}), mrepo=mrepo, urepo=FakeUserRepo({9}))
    await svc.update_role("p", 9, "owner")
    assert mrepo.role_updates == [("p", 9, "owner")]


# --------------------------------------------------------------------------- #
# relationships
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_related_files_delegates():
    out = await _svc().get_related_files("p", "rel-1")
    assert out == [{"file_id": "a", "relationship_id": "rel-1"}]


@pytest.mark.asyncio
async def test_get_file_ancestors_missing_file_404():
    svc = _svc(drepo=FakeDocumentRepo(files=set()))
    with pytest.raises(NotFoundError):
        await svc.get_file_ancestors("p", "f")


@pytest.mark.asyncio
async def test_get_file_ancestors_success():
    svc = _svc(drepo=FakeDocumentRepo(files={("f", "p")}))
    out = await svc.get_file_ancestors("p", "f")
    assert out[-1]["file_id"] == "f"
