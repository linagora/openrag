"""A partition keeps the embedder its files were built with (#762), against a real Postgres.

A partition on the ``default`` embedder alias follows every change of default.
That is the point of the alias while the partition is empty, and silent
corruption once it holds vectors. These tests pin down the SQL that keeps the
two apart: the first write pins a partition by name, a change of default pins
the indexed partitions still on the alias, and deleting the default is refused
only for partitions with something to lose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.config.model_endpoints import ModelEndpointRow
from core.utils.exceptions import ConflictError
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _endpoint(name: str, *, is_default: bool) -> ModelEndpointRow:
    now = datetime.now(UTC)
    return ModelEndpointRow(
        name=name,
        model_type="embedder",
        endpoint=f"http://{name}:8000/v1",
        model_name=name,
        is_default=is_default,
        created_at=now,
        updated_at=now,
    )


async def _embedders(store: PostgresStore, *names: str, default: str) -> None:
    """Replace every embedder endpoint with *names*, *default* marked default."""
    async with store.pool.acquire() as conn:
        await conn.execute("DELETE FROM model_endpoints WHERE model_type = 'embedder'")
    for name in names:
        await store.model_endpoint_repo.create(_endpoint(name, is_default=name == default))


async def _partition(store: PostgresStore, name: str, *, embedder: str = "default", files: int = 0) -> None:
    await store.partition_repo.create_partition(name)
    await store.partition_repo.update_partition(name, embedder=embedder)
    for i in range(files):
        await store.document_repo.add_file_to_partition(file_id=f"{name}-{i}", partition=name)


async def _embedder_of(store: PostgresStore, partition: str) -> str:
    row = await store.partition_repo.get_partition_row(partition)
    return row["embedder"]


class TestPinOnFirstWrite:
    async def test_pins_an_alias_partition_to_the_current_default(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "docs")

        assert await postgres_store.partition_repo.pin_default_embedder("docs") == "jina"
        assert await _embedder_of(postgres_store, "docs") == "jina"

    async def test_leaves_an_explicit_embedder_alone(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "docs", embedder="bge")

        assert await postgres_store.partition_repo.pin_default_embedder("docs") == "bge"
        assert await _embedder_of(postgres_store, "docs") == "bge"

    async def test_is_a_no_op_the_second_time(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "docs")
        await postgres_store.partition_repo.pin_default_embedder("docs")
        await postgres_store.model_endpoint_repo.set_default("embedder", "bge")

        # Pinned before the default moved, so it stays where its files are.
        assert await postgres_store.partition_repo.pin_default_embedder("docs") == "jina"

    async def test_reports_a_missing_partition_as_none(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")

        assert await postgres_store.partition_repo.pin_default_embedder("ghost") is None

    async def test_pins_on_the_admission_lock_connection(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")
        await _partition(postgres_store, "docs")

        async with postgres_store.partition_repo.partition_operation_lock("docs") as operation:
            assert await operation.pin_default_embedder("docs") == "jina"

        assert await _embedder_of(postgres_store, "docs") == "jina"


class TestChangeOfDefault:
    async def test_set_default_moves_empty_alias_partitions_and_keeps_indexed_ones(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "empty")
        await _partition(postgres_store, "indexed", files=2)
        await _partition(postgres_store, "explicit", embedder="jina", files=1)

        await postgres_store.model_endpoint_repo.set_default("embedder", "bge")

        assert await _embedder_of(postgres_store, "empty") == "default"
        assert await _embedder_of(postgres_store, "indexed") == "jina"
        assert await _embedder_of(postgres_store, "explicit") == "jina"

    async def test_creating_a_new_default_keeps_indexed_alias_partitions(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")
        await _partition(postgres_store, "empty")
        await _partition(postgres_store, "indexed", files=1)

        await postgres_store.model_endpoint_repo.create(_endpoint("bge", is_default=True))

        assert await _embedder_of(postgres_store, "empty") == "default"
        assert await _embedder_of(postgres_store, "indexed") == "jina"

    async def test_a_failed_create_pins_nothing(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "indexed", files=1)

        with pytest.raises(Exception, match="already exists"):
            await postgres_store.model_endpoint_repo.create(_endpoint("bge", is_default=True))

        assert await _embedder_of(postgres_store, "indexed") == "default"

    async def test_setting_the_current_default_again_pins_nothing(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "indexed", files=1)

        await postgres_store.model_endpoint_repo.set_default("embedder", "jina")

        assert await _embedder_of(postgres_store, "indexed") == "default"


class TestDeleteTheDefault:
    async def test_empty_alias_partitions_follow_the_promoted_default(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "bge", "jina", default="jina")
        await _partition(postgres_store, "empty")

        status, promoted = await postgres_store.model_endpoint_repo.delete_and_promote_default("jina", "embedder")

        assert (status, promoted) == ("ok", "bge")
        assert await _embedder_of(postgres_store, "empty") == "default"

    async def test_indexed_alias_partitions_still_refuse_the_delete(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "bge", "jina", default="jina")
        await _partition(postgres_store, "empty")
        await _partition(postgres_store, "indexed", files=1)

        with pytest.raises(ConflictError) as exc:
            await postgres_store.model_endpoint_repo.delete_and_promote_default("jina", "embedder")

        assert "1 follow the 'default' alias with indexed files" in exc.value.message
        assert await postgres_store.model_endpoint_repo.get("jina", "embedder") is not None
