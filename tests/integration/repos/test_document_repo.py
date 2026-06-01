"""Phase 7F — PgDocumentRepository against a real Postgres."""

from __future__ import annotations

import pytest
from core.models.catalog import DocumentRecord, DocumentStatus
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _seed_partition(store: PostgresStore, name: str = "p") -> str:
    """``files`` has an FK to ``partitions``; the partition row must exist first."""
    await store.partition_repo.create_partition(name)
    return name


def _doc(file_id: str, partition: str = "p", **extra) -> DocumentRecord:
    return DocumentRecord(
        id=file_id,
        file_id=file_id,
        partition=partition,
        filename=f"{file_id}.pdf",
        **extra,
    )


class TestCreateGetDelete:
    async def test_create_then_get(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store)
        await postgres_store.document_repo.create_document(_doc("f1", partition))
        fetched = await postgres_store.document_repo.get_document("f1")
        assert fetched is not None
        assert fetched.file_id == "f1"
        assert fetched.partition == partition
        assert fetched.filename == "f1.pdf"

    async def test_get_missing_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.document_repo.get_document("nope") is None

    async def test_delete_returns_true_on_success(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store)
        await postgres_store.document_repo.create_document(_doc("f2", partition))
        assert await postgres_store.document_repo.delete_document("f2") is True
        assert await postgres_store.document_repo.get_document("f2") is None

    async def test_delete_missing_returns_false(self, postgres_store: PostgresStore):
        assert await postgres_store.document_repo.delete_document("ghost") is False


class TestListFilter:
    async def test_list_by_partition(self, postgres_store: PostgresStore):
        await _seed_partition(postgres_store, "alpha")
        await _seed_partition(postgres_store, "beta")
        repo = postgres_store.document_repo
        await repo.create_document(_doc("a1", "alpha"))
        await repo.create_document(_doc("a2", "alpha"))
        await repo.create_document(_doc("b1", "beta"))

        only_alpha = await repo.list_documents(partition="alpha")
        assert {d.file_id for d in only_alpha} == {"a1", "a2"}

    async def test_list_by_partition_list(self, postgres_store: PostgresStore):
        await _seed_partition(postgres_store, "alpha")
        await _seed_partition(postgres_store, "beta")
        repo = postgres_store.document_repo
        await repo.create_document(_doc("a1", "alpha"))
        await repo.create_document(_doc("b1", "beta"))
        both = await repo.list_documents(partition=["alpha", "beta"])
        assert {d.file_id for d in both} == {"a1", "b1"}

    async def test_count_documents(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store)
        repo = postgres_store.document_repo
        assert await repo.count_documents(partition=partition) == 0
        await repo.create_document(_doc("c1", partition))
        await repo.create_document(_doc("c2", partition))
        assert await repo.count_documents(partition=partition) == 2

    async def test_file_exists_in_partition(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store)
        repo = postgres_store.document_repo
        assert await repo.file_exists_in_partition("e1", partition) is False
        await repo.create_document(_doc("e1", partition))
        assert await repo.file_exists_in_partition("e1", partition) is True


class TestUpdate:
    async def test_update_status_folds_into_metadata(
        self,
        postgres_store: PostgresStore,
    ):
        partition = await _seed_partition(postgres_store)
        repo = postgres_store.document_repo
        await repo.create_document(_doc("u1", partition))

        updated = await repo.update_document("u1", status=DocumentStatus.COMPLETED)
        assert updated is not None
        assert updated.status == DocumentStatus.COMPLETED

    async def test_update_metadata_merges(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store)
        repo = postgres_store.document_repo
        await repo.create_document(
            _doc("u2", partition, metadata={"a": 1, "b": 2}),
        )
        updated = await repo.update_document("u2", metadata={"b": 99, "c": 3})
        assert updated is not None
        # filename / status / error_message live in their own DocumentRecord
        # fields after the row → domain conversion lifts them out of the JSON.
        assert updated.filename == "u2.pdf"
        assert updated.metadata == {"a": 1, "b": 99, "c": 3}

    async def test_update_missing_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.document_repo.update_document("nope") is None


class TestDeleteByPartition:
    async def test_returns_deletion_count(self, postgres_store: PostgresStore):
        partition = await _seed_partition(postgres_store, "trash")
        repo = postgres_store.document_repo
        await repo.create_document(_doc("d1", partition))
        await repo.create_document(_doc("d2", partition))
        deleted = await repo.delete_documents_by_partition(partition)
        assert deleted == 2
        assert await repo.count_documents(partition=partition) == 0
