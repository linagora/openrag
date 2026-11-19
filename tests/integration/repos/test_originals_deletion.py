"""Uploaded originals go with their file, against a real catalog and real files."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.models.workspace import Workspace
from services.orchestrators.partition_service import PartitionService
from services.orchestrators.workspace_service import WorkspaceService
from services.storage.originals import OriginalsCleaner, markdown_sidecar_path
from services.storage.orphan_originals import scan_orphan_originals
from services.workers.indexer_actor import IndexerWorker, _write_catalog_record

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def original(data_dir: Path, name: str) -> str:
    path = data_dir / name
    path.write_bytes(b"content")
    markdown_sidecar_path(path).write_text("# parsed")
    return str(path.resolve())


async def upload(store, file_id: str, source: str, *, workspace_ids=None, partition: str = "p") -> None:
    assert await _write_catalog_record(
        doc_repo=store.document_repo,
        metadata={"file_id": file_id, "source": source},
        partition=partition,
        user=None,
        replace=False,
        indexation_config=None,
        workspace_ids=workspace_ids,
    )
    for workspace_id in workspace_ids or []:
        assert await store.workspace_repo.add_files_to_workspace(partition, workspace_id, [file_id]) == []
    if workspace_ids:
        assert await store.document_repo.finalize_file_workspace_ownership(file_id, partition, workspace_ids)


def workspace_service(store, data_dir: Path) -> WorkspaceService:
    vectors = AsyncMock()
    vectors.query_ids_by_filter.side_effect = lambda collection, filters: [filters["file_id"] + "-chunk"]
    return WorkspaceService(
        workspace_repo=store.workspace_repo,
        document_repo=store.document_repo,
        vector_store=vectors,
        collection="test",
        originals_cleaner=OriginalsCleaner(store.document_repo, data_dir),
    )


async def test_deleting_a_workspace_deletes_its_orphans_originals_but_not_a_copys(postgres_store, tmp_path):
    store = postgres_store
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await store.partition_repo.create_partition("p")
    await store.partition_repo.create_partition("q")
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws1", partition="p"))

    alone = original(data_dir, "1_alone.pdf")
    shared = original(data_dir, "2_shared.pdf")
    await upload(store, "alone", alone, workspace_ids=["ws1"])
    await upload(store, "shared", shared, workspace_ids=["ws1"])
    # A copy, in another partition, keeps its source's ``source``.
    await upload(store, "shared-copy", shared, partition="q")

    result = await workspace_service(store, data_dir).delete_workspace("p", "ws1")

    assert result["orphaned_files_deleted"] == 2
    assert not Path(alone).exists() and not markdown_sidecar_path(alone).exists()
    assert Path(shared).exists() and markdown_sidecar_path(shared).exists()

    # Once the copy goes too, nothing references the original any more.
    await store.document_repo.remove_file_from_partition("shared-copy", "q")
    assert await OriginalsCleaner(store.document_repo, data_dir).delete_unreferenced([shared]) == 1
    assert sorted(p.name for p in data_dir.iterdir()) == []


async def test_a_file_kept_by_keep_files_keeps_its_original(postgres_store, tmp_path):
    store = postgres_store
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await store.partition_repo.create_partition("p")
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws1", partition="p"))
    kept = original(data_dir, "1_kept.pdf")
    await upload(store, "kept", kept, workspace_ids=["ws1"])

    await workspace_service(store, data_dir).delete_workspace("p", "ws1", keep_files=True)

    assert Path(kept).exists()
    assert await store.document_repo.file_exists_in_partition("kept", "p")


async def test_deleting_a_partition_deletes_its_originals_but_not_a_copys(postgres_store, tmp_path):
    store = postgres_store
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await store.partition_repo.create_partition("p")
    await store.partition_repo.create_partition("q")
    alone = original(data_dir, "1_alone.pdf")
    shared = original(data_dir, "2_shared.pdf")
    await upload(store, "alone", alone)
    await upload(store, "shared", shared)
    await upload(store, "shared-copy", shared, partition="q")

    vectors = AsyncMock()
    vectors.collection_exists.return_value = False
    service = PartitionService(
        partition_repo=store.partition_repo,
        membership_repo=store.membership_repo,
        document_repo=store.document_repo,
        vector_store=vectors,
        user_repo=store.user_repo,
        collection="test",
        originals_cleaner=OriginalsCleaner(store.document_repo, data_dir),
    )
    await service.delete_partition("p")
    await asyncio.gather(*list(service._original_cleanups))

    assert not Path(alone).exists() and not markdown_sidecar_path(alone).exists()
    assert Path(shared).exists() and markdown_sidecar_path(shared).exists()
    assert await store.document_repo.file_exists_in_partition("shared-copy", "q")


def replacing_worker(store, cleaner: OriginalsCleaner) -> IndexerWorker:
    class StoredPipeline:
        async def run(self, row):
            row["stored_count"] = 1
            row["stage"] = "stored"
            return row

    tsm = MagicMock()
    tsm.set_state.remote = AsyncMock(return_value=None)
    tsm.complete_with_degraded_stages.remote = AsyncMock(return_value="completed")
    return IndexerWorker(
        pipeline=StoredPipeline(),
        task_state_manager=tsm,
        document_repo=store.document_repo,
        originals_cleaner=cleaner,
    )


async def test_a_replace_deletes_the_previous_original_but_not_a_copys(postgres_store, tmp_path):
    store = postgres_store
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await store.partition_repo.create_partition("p")
    await store.partition_repo.create_partition("q")
    old = original(data_dir, "1_old.pdf")
    copied = original(data_dir, "2_copied.pdf")
    await upload(store, "f", old)
    await upload(store, "g", copied)
    await upload(store, "g-copy", copied, partition="q")
    cleaner = OriginalsCleaner(store.document_repo, data_dir)

    for file_id in ("f", "g"):
        new = original(data_dir, f"3_{file_id}_new.txt")
        await replacing_worker(store, cleaner).process_file(
            task_id=f"t-{file_id}",
            path=new,
            metadata={"file_id": file_id, "source": new},
            partition="p",
            replace=True,
        )
        assert (await store.document_repo.get_file_metadata(file_id, "p"))["source"] == new
        assert Path(new).exists()

    assert not Path(old).exists() and not markdown_sidecar_path(old).exists()
    assert Path(copied).exists() and markdown_sidecar_path(copied).exists()


async def test_the_orphan_scan_deletes_only_what_no_row_references(postgres_store, tmp_path):
    store = postgres_store
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await store.partition_repo.create_partition("p")
    await store.partition_repo.create_partition("q")
    kept = original(data_dir, "1713700000000_a1b2_kept.pdf")
    copied = original(data_dir, "1713700000001_c3d4_copied.pdf")
    orphan = original(data_dir, "1713700000002_e5f6_deleted_before.pdf")
    young = original(data_dir, "1713700000003_aaaa_in_flight.pdf")
    await upload(store, "kept", kept)
    await upload(store, "copy", copied, partition="q")
    day_ago = time.time() - 24 * 3600 - 60
    for path in (kept, copied, orphan):
        for target in (path, str(markdown_sidecar_path(path))):
            os.utime(target, (day_ago, day_ago))

    async def scan(**kwargs):
        return [
            e async for e in scan_orphan_originals(store.document_repo, data_dir, min_age_seconds=24 * 3600, **kwargs)
        ]

    events = await scan(delete=True)

    assert [e["path"] for e in events if e["type"] == "orphan"] == [orphan, str(markdown_sidecar_path(orphan))]
    assert events[-1]["deleted"] == 2 and events[-1]["referenced"] == 4 and events[-1]["young"] == 2
    assert not Path(orphan).exists() and not markdown_sidecar_path(orphan).exists()
    assert all(Path(path).exists() and markdown_sidecar_path(path).exists() for path in (kept, copied, young))
    assert (await scan())[-1]["orphans"] == 0
