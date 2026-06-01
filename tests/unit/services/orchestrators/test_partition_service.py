"""Unit tests for :class:`PartitionService` (Phase 8B.1)."""

from __future__ import annotations

import pytest
from core.utils.exceptions import NotFoundError, PartitionNotFoundError, UserNotFoundError, ValidationError
from services.orchestrators.partition_service import PartitionService


class FakePartitionRepo:
    def __init__(self, existing: set[str] | None = None):
        self._existing = existing if existing is not None else set()
        self.created: list[tuple[str, int]] = []
        self.deleted: list[str] = []

    async def partition_exists(self, name: str) -> bool:
        return name in self._existing

    async def list_partitions(self) -> list[dict]:
        return [{"partition": p} for p in sorted(self._existing)]

    async def create_partition(self, name: str, user_id: int | None = None) -> dict:
        self._existing.add(name)
        self.created.append((name, user_id))
        return {"partition": name}

    async def delete_partition(self, name: str) -> bool:
        self.deleted.append(name)
        self._existing.discard(name)
        return True


class FakeMembershipRepo:
    def __init__(self, members: set[tuple[int, str]] | None = None):
        self._members = members or set()
        self.added: list[tuple[str, int, str]] = []
        self.removed: list[tuple[str, int]] = []
        self.role_updates: list[tuple[str, int, str]] = []

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
    def __init__(self, ids=None, rows=None):
        self._ids = ids or []
        self._rows = rows or []
        self.deleted_ids: list[str] = []

    async def query_ids_by_filter(self, collection, filters):
        return list(self._ids)

    async def delete(self, ids, collection="default") -> int:
        self.deleted_ids.extend(ids)
        return len(ids)

    async def query_chunks_by_filter(self, collection, filters, output_fields=None):
        return list(self._rows)


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
) -> PartitionService:
    return PartitionService(
        partition_repo=prepo or FakePartitionRepo(),
        membership_repo=mrepo or FakeMembershipRepo(),
        document_repo=drepo or FakeDocumentRepo(),
        vector_store=vstore or FakeVectorStore(),
        user_repo=urepo or FakeUserRepo(),
        collection=collection,
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
async def test_create_partition_success():
    prepo = FakePartitionRepo()
    await _svc(prepo=prepo).create_partition("new", 7)
    assert prepo.created == [("new", 7)]


@pytest.mark.asyncio
async def test_delete_partition_missing_raises_404():
    with pytest.raises(PartitionNotFoundError):
        await _svc(prepo=FakePartitionRepo(existing=set())).delete_partition("ghost")


@pytest.mark.asyncio
async def test_delete_partition_drops_vectors_then_rows():
    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore(ids=["c1", "c2"])
    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")
    assert vstore.deleted_ids == ["c1", "c2"]
    assert prepo.deleted == ["p1"]


@pytest.mark.asyncio
async def test_delete_partition_no_vectors_still_deletes_rows():
    prepo = FakePartitionRepo(existing={"p1"})
    vstore = FakeVectorStore(ids=[])
    await _svc(prepo=prepo, vstore=vstore).delete_partition("p1")
    assert vstore.deleted_ids == []
    assert prepo.deleted == ["p1"]


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
    rows = [{"_id": str(i), "text": "body", "page": i} for i in range(5)]
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
    rows = [{"text": "t", "_id": "1", "vector": [0.1, 0.2]}]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    out = await svc.list_all_chunks("p", include_embedding=False)
    assert out[0]["content"] == "t"
    assert "vector" not in out[0]["metadata"]
    assert "text" not in out[0]["metadata"]


@pytest.mark.asyncio
async def test_list_all_chunks_stringifies_vector_when_included():
    rows = [{"text": "t", "_id": "1", "vector": [0.1, 0.2]}]
    svc = _svc(prepo=FakePartitionRepo({"p"}), vstore=FakeVectorStore(rows=rows))
    out = await svc.list_all_chunks("p", include_embedding=True)
    assert isinstance(out[0]["metadata"]["vector"], str)


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
