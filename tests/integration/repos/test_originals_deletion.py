"""Uploaded originals go with their file, against a real catalog and real files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from core.models.workspace import Workspace
from services.orchestrators.workspace_service import WorkspaceService
from services.storage.originals import OriginalsCleaner, markdown_sidecar_path
from services.workers.indexer_actor import _write_catalog_record

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
