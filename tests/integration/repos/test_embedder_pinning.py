"""A partition keeps the embedder its files were built with (#762), against a real Postgres.

A partition on the ``default`` embedder alias follows every change of default.
That is the point of the alias while the partition is empty, and silent
corruption once it holds vectors. These tests pin down the SQL that keeps the
two apart: the first write pins a partition by name, a change of default pins
the indexed partitions still on the alias, and deleting the default is refused
only for partitions with something to lose.

``TestEditRacingIndexing`` covers the other way vectors and their embedder can
part: an endpoint edited while a file is still indexing against it (#958).
"""

from __future__ import annotations

import asyncio
import functools
from datetime import UTC, datetime

import pytest
from core.config.model_endpoints import ModelEndpointRow, embedder_fingerprint
from core.utils.exceptions import ConflictError
from services.orchestrators.model_endpoint_service import _refuse_unacknowledged_repoint
from services.persistence.document_repo import _refuse_if_embedder_changed
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

        field = (await postgres_store.model_endpoint_repo.get("jina", "embedder")).vector_field
        result = await postgres_store.model_endpoint_repo.delete_and_promote_default("jina", "embedder")

        assert result == ("ok", "bge", field)
        assert await _embedder_of(postgres_store, "empty") == "default"

    async def test_indexed_alias_partitions_still_refuse_the_delete(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "bge", "jina", default="jina")
        await _partition(postgres_store, "empty")
        await _partition(postgres_store, "indexed", files=1)

        with pytest.raises(ConflictError) as exc:
            await postgres_store.model_endpoint_repo.delete_and_promote_default("jina", "embedder")

        assert "1 follow the 'default' alias with indexed files" in exc.value.message
        assert await postgres_store.model_endpoint_repo.get("jina", "embedder") is not None


def _built_with(name: str) -> dict[str, str | None]:
    """The fingerprint the indexer stamps on a client built from ``_endpoint(name)``."""
    return embedder_fingerprint(f"http://{name}:8000/v1", name, {})


def _edit_guard(**fields):
    """The service's acknowledgement guard, for the fields an edit changes."""
    return functools.partial(_refuse_unacknowledged_repoint, fields=fields)


async def _still_waiting(task: asyncio.Task) -> bool:
    await asyncio.sleep(0.3)
    return not task.done()


class TestEditRacingIndexing:
    """An embedder edit and a file being indexed against it (#958).

    The vectors are stored before the catalog row, so the edit guard, which
    counts catalog rows, cannot see a file still in flight. The catalog write
    locks the partition's endpoint row FOR SHARE and the edit locks it FOR
    UPDATE, so whichever commits second sees the first.
    """

    async def test_a_file_recorded_after_an_edit_is_refused(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")
        await _partition(postgres_store, "docs", embedder="jina")

        # Nothing indexed yet, so the edit needs no acknowledgement...
        await postgres_store.model_endpoint_repo.update(
            "jina", "embedder", guard=_edit_guard(model_name="jina-v4"), model_name="jina-v4"
        )
        # ...and the file embedded before it cannot be recorded against it.
        with pytest.raises(ConflictError) as exc:
            await postgres_store.document_repo.add_file_to_partition(
                file_id="f1", partition="docs", embedder_fingerprint=_built_with("jina")
            )

        assert exc.value.code == "EMBEDDER_CHANGED_DURING_INDEXING"
        assert not await postgres_store.document_repo.file_exists_in_partition("f1", "docs")

    async def test_a_file_waits_for_an_edit_in_flight_and_sees_it(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")
        await _partition(postgres_store, "docs", embedder="jina")

        async with postgres_store.pool.acquire() as edit:
            tx = edit.transaction()
            await tx.start()
            await edit.execute("SELECT 1 FROM model_endpoints WHERE name = 'jina' FOR UPDATE")
            await edit.execute("UPDATE model_endpoints SET model_name = 'jina-v4' WHERE name = 'jina'")
            record = asyncio.create_task(
                postgres_store.document_repo.add_file_to_partition(
                    file_id="f1", partition="docs", embedder_fingerprint=_built_with("jina")
                )
            )
            assert await _still_waiting(record)
            await tx.commit()

        with pytest.raises(ConflictError) as exc:
            await record
        assert exc.value.code == "EMBEDDER_CHANGED_DURING_INDEXING"

    async def test_an_edit_waits_for_a_file_being_recorded_and_counts_it(self, postgres_store: PostgresStore):
        await _embedders(postgres_store, "jina", default="jina")
        await _partition(postgres_store, "docs", embedder="jina")

        async with postgres_store.pool.acquire() as indexer:
            tx = indexer.transaction()
            await tx.start()
            await _refuse_if_embedder_changed(indexer, "docs", _built_with("jina"))
            await indexer.execute("INSERT INTO files (file_id, partition_name) VALUES ('f1', 'docs')")
            edit = asyncio.create_task(
                postgres_store.model_endpoint_repo.update(
                    "jina", "embedder", guard=_edit_guard(model_name="jina-v4"), model_name="jina-v4"
                )
            )
            assert await _still_waiting(edit)
            await tx.commit()

        with pytest.raises(ConflictError) as exc:
            await edit
        assert exc.value.code == "EMBEDDER_EDIT_AFFECTS_INDEXED_DATA"
        assert (await postgres_store.model_endpoint_repo.get("jina", "embedder")).model_name == "jina"

    async def test_a_change_of_default_waits_for_a_file_being_recorded(self, postgres_store: PostgresStore):
        """set_default pins the alias partitions with files, so it and a catalog
        write each want what the other locks. Both take `partitions` first, so
        one waits instead of the two deadlocking."""
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "legacy", files=1)

        async with postgres_store.pool.acquire() as indexer:
            tx = indexer.transaction()
            await tx.start()
            await _refuse_if_embedder_changed(indexer, "legacy", _built_with("jina"))
            change = asyncio.create_task(postgres_store.model_endpoint_repo.set_default("embedder", "bge"))
            assert await _still_waiting(change)
            await indexer.execute("INSERT INTO files (file_id, partition_name) VALUES ('f2', 'legacy')")
            await tx.commit()

        await asyncio.wait_for(change, timeout=5)
        assert await _embedder_of(postgres_store, "legacy") == "jina"

    async def test_a_reindex_waits_for_a_change_of_default_and_is_recorded_on_the_pin(
        self, postgres_store: PostgresStore
    ):
        """The other order: the change of default holds `partitions` and the
        endpoint rows, and pins the partition a file is being re-indexed into.
        A re-index writes no `partitions` row of its own, so it has to lock the
        table itself before the partition row, or the two deadlock."""
        await _embedders(postgres_store, "jina", "bge", default="jina")
        await _partition(postgres_store, "legacy", files=1)

        async with postgres_store.pool.acquire() as change:
            tx = change.transaction()
            await tx.start()
            # set_default's own order: partitions, the endpoint rows, then the pin.
            await change.execute("LOCK TABLE partitions IN SHARE ROW EXCLUSIVE MODE")
            await change.execute("SELECT 1 FROM model_endpoints WHERE model_type = 'embedder' FOR UPDATE")
            reindex = asyncio.create_task(
                postgres_store.document_repo.update_file_in_partition(
                    "legacy-0", "legacy", indexation_config={}, embedder_fingerprint=_built_with("jina")
                )
            )
            assert await _still_waiting(reindex)
            await change.execute("UPDATE partitions SET embedder = 'jina' WHERE partition = 'legacy'")
            await change.execute("UPDATE model_endpoints SET is_default = (name = 'bge') WHERE model_type = 'embedder'")
            await tx.commit()

        assert await asyncio.wait_for(reindex, timeout=5) is True
