"""Unit tests for :class:`IndexingService` (Phase 8D.1)."""

from __future__ import annotations

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.utils.exceptions import PartitionNotFoundError, ValidationError
from services.orchestrators.indexing_service import IndexingService


class FakeDocumentRepo:
    def __init__(self, *, exists: bool = False, raise_on_check: bool = False):
        self._exists = exists
        self._raise = raise_on_check

    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool:
        if self._raise:
            raise RuntimeError("boom")
        return self._exists


class FakeWorkspaceRepo:
    def __init__(self, *, workspace=None):
        self._workspace = workspace

    async def get_workspace_dict(self, workspace_id: str):
        return self._workspace


class FakeDispatcher:
    def __init__(self):
        self.dispatched: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[tuple] = []
        self.copied: list[tuple] = []
        self.cancelled: list[str] = []
        self.cancel_result = True

    async def dispatch_indexing(
        self,
        *,
        path,
        metadata,
        partition,
        user,
        workspace_ids,
        replace,
        indexation_config=None,
        embedder_name=None,
    ):
        self.dispatched.append(
            {
                "path": path,
                "metadata": metadata,
                "partition": partition,
                "user": user,
                "workspace_ids": workspace_ids,
                "replace": replace,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
            }
        )
        return "task-abc"

    async def delete_file(self, file_id, partition):
        self.deleted.append((file_id, partition))

    async def update_file_metadata(self, file_id, metadata, partition, user):
        self.updated.append((file_id, metadata, partition, user))

    async def copy_file(self, file_id, metadata, partition, user):
        self.copied.append((file_id, metadata, partition, user))

    async def get_task_state(self, task_id):
        return "QUEUED"

    async def get_task_error(self, task_id):
        return "trace"

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return self.cancel_result


def _config_with_partition(partition: str = "tenant-a"):
    return type(
        "Config",
        (),
        {
            "partitions": {
                partition: PartitionConfig(
                    name=partition,
                    embedder="embed-fast",
                    indexation=IndexationPipelineConfig(
                        parsing_strategy="pymupdf",
                        enable_image_captioning=False,
                        enable_contextualization=True,
                        contextualization_llm="llm-context",
                    ),
                    retrieval=RetrievalPipelineConfig(),
                )
            }
        },
    )()


class FakePartitionService:
    """Minimal partition service: records creates and mutates the config cache."""

    def __init__(self, config, *, db_partitions: set[str] | None = None):
        self._config = config
        self._db = db_partitions or set()
        self._members: dict[str, list[dict]] = {}
        self.created: list[tuple[str, int]] = []
        self.create_kwargs: list[dict] = []
        self.loaded = 0

    def _cfg(self, partition: str) -> PartitionConfig:
        return PartitionConfig(
            name=partition,
            embedder="default",
            indexation=IndexationPipelineConfig(),
            retrieval=RetrievalPipelineConfig(),
        )

    async def partition_exists(self, partition: str) -> bool:
        return partition in self._db

    async def create_partition(self, partition: str, *, user_id: int, **_) -> None:
        self.created.append((partition, user_id))
        self.create_kwargs.append(dict(_))
        self._db.add(partition)
        self._members.setdefault(partition, []).append({"user_id": user_id, "role": "owner"})
        # Mimic create_partition + load_partitions populating the cache.
        self._config.partitions[partition] = self._cfg(partition)

    async def load_partitions(self) -> None:
        self.loaded += 1
        # Mimic the cache being rebuilt from all DB rows.
        for name in self._db:
            self._config.partitions.setdefault(name, self._cfg(name))

    async def list_members(self, partition: str) -> list[dict]:
        return list(self._members.get(partition, []))


class RaceLostPartitionService(FakePartitionService):
    def __init__(self, config, *, grant_owner: bool = True):
        super().__init__(config)
        self.grant_owner = grant_owner

    async def create_partition(self, partition: str, *, user_id: int, **_) -> None:
        self.created.append((partition, user_id))
        self.create_kwargs.append(dict(_))
        self._db.add(partition)
        if self.grant_owner:
            self._members.setdefault(partition, []).append({"user_id": user_id, "role": "owner"})
        raise ValidationError(
            f"Partition '{partition}' already exists.",
            status_code=409,
            code="PARTITION_EXISTS",
        )


def _service(*, doc=None, ws=None, disp=None, config=None, partition_service=None):
    return IndexingService(
        document_repo=doc or FakeDocumentRepo(),
        workspace_repo=ws or FakeWorkspaceRepo(),
        dispatcher=disp or FakeDispatcher(),
        config=config,
        partition_service=partition_service,
    )


@pytest.mark.asyncio
async def test_file_exists_passthrough():
    svc = _service(doc=FakeDocumentRepo(exists=True))
    assert await svc.file_exists("f1", "p1") is True


@pytest.mark.asyncio
async def test_file_exists_swallows_errors():
    svc = _service(doc=FakeDocumentRepo(raise_on_check=True))
    assert await svc.file_exists("f1", "p1") is False


@pytest.mark.asyncio
async def test_get_workspace_passthrough():
    ws = {"workspace_id": "w1", "partition_name": "p1"}
    svc = _service(ws=FakeWorkspaceRepo(workspace=ws))
    assert await svc.get_workspace("w1") == ws


@pytest.mark.asyncio
async def test_add_file_builds_metadata_and_dispatches(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world")
    disp = FakeDispatcher()
    svc = _service(disp=disp)

    task_id = await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={"author": "alice"},
        sanitized_filename="doc.txt",
        original_filename="Doc Original.txt",
        user={"id": 7},
        workspace_ids=["w1"],
    )

    assert task_id == "task-abc"
    assert len(disp.dispatched) == 1
    sent = disp.dispatched[0]
    assert sent["path"] == str(f)
    assert sent["partition"] == "p1"
    assert sent["workspace_ids"] == ["w1"]
    assert sent["replace"] is False
    md = sent["metadata"]
    assert md["author"] == "alice"
    assert md["source"] == str(f)
    assert md["filename"] == "doc.txt"
    assert md["original_filename"] == "Doc Original.txt"
    assert md["file_id"] == "f1"
    assert md["file_size"] == "11.00 B"


@pytest.mark.asyncio
async def test_replace_sets_replace_flag(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
        replace=True,
    )
    assert disp.dispatched[0]["replace"] is True


@pytest.mark.asyncio
async def test_add_file_dispatches_partition_indexation_config_and_embedder(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    svc = _service(disp=disp, config=_config_with_partition("tenant-a"))

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
    )

    sent = disp.dispatched[0]
    assert sent["embedder_name"] == "embed-fast"
    assert sent["indexation_config"]["parsing_strategy"] == "pymupdf"
    assert sent["indexation_config"]["enable_image_captioning"] is False
    assert sent["indexation_config"]["enable_contextualization"] is True
    assert sent["indexation_config"]["contextualization_llm"] == "llm-context"


@pytest.mark.asyncio
async def test_add_file_rejects_unknown_partition_when_partition_configs_exist(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    svc = _service(config=_config_with_partition("tenant-a"))

    with pytest.raises(PartitionNotFoundError, match="tenant-b"):
        await svc.add_file(
            file_path=str(f),
            file_id="f1",
            partition="tenant-b",
            metadata={},
            sanitized_filename="doc.txt",
            original_filename="doc.txt",
            user=None,
        )


@pytest.mark.asyncio
async def test_add_file_auto_creates_unknown_partition_when_service_wired(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = _config_with_partition("tenant-a")
    psvc = FakePartitionService(config)
    svc = _service(disp=disp, config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-new",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
    )

    # Partition was created with the uploader as owner, then the file dispatched.
    assert psvc.created == [("tenant-new", 7)]
    assert psvc.create_kwargs == [{"max_owned": 100}]
    assert disp.dispatched[0]["partition"] == "tenant-new"


@pytest.mark.asyncio
async def test_add_file_auto_create_defaults_to_admin_when_user_missing(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    config = _config_with_partition("tenant-a")
    psvc = FakePartitionService(config)
    svc = _service(config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-new",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
    )

    assert psvc.created == [("tenant-new", 1)]
    assert psvc.create_kwargs == [{"max_owned": None}]


@pytest.mark.asyncio
async def test_add_file_auto_create_preserves_admin_cap_bypass(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    config = _config_with_partition("tenant-a")
    psvc = FakePartitionService(config)
    svc = _service(config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-new",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7, "is_admin": True},
    )

    assert psvc.created == [("tenant-new", 7)]
    assert psvc.create_kwargs == [{"max_owned": None}]


@pytest.mark.asyncio
async def test_add_file_refreshes_cache_when_partition_row_exists(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = _config_with_partition("tenant-a")
    # Row exists in the DB but is missing from the in-memory cache.
    psvc = FakePartitionService(config, db_partitions={"tenant-new"})
    svc = _service(disp=disp, config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-new",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
    )

    # No spurious create; the stale cache was refreshed and the file dispatched.
    assert psvc.created == []
    assert psvc.loaded == 1
    assert disp.dispatched[0]["partition"] == "tenant-new"


@pytest.mark.asyncio
async def test_add_file_treats_partition_exists_race_as_success(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = _config_with_partition("tenant-a")
    psvc = RaceLostPartitionService(config)
    svc = _service(disp=disp, config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-new",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
    )

    assert psvc.created == [("tenant-new", 7)]
    assert psvc.create_kwargs == [{"max_owned": 100}]
    assert psvc.loaded == 1
    assert disp.dispatched[0]["partition"] == "tenant-new"


@pytest.mark.asyncio
async def test_add_file_rejects_partition_exists_race_without_membership(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = _config_with_partition("tenant-a")
    psvc = RaceLostPartitionService(config, grant_owner=False)
    svc = _service(disp=disp, config=config, partition_service=psvc)

    with pytest.raises(PermissionError, match="Editor role required"):
        await svc.add_file(
            file_path=str(f),
            file_id="f1",
            partition="tenant-new",
            metadata={},
            sanitized_filename="doc.txt",
            original_filename="doc.txt",
            user={"id": 7},
        )

    assert disp.dispatched == []


@pytest.mark.asyncio
async def test_delete_file_delegates():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.delete_file("f1", "p1")
    assert disp.deleted == [("f1", "p1")]


@pytest.mark.asyncio
async def test_update_metadata_injects_file_id():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.update_metadata("f1", {"author": "bob"}, "p1", {"id": 1})
    file_id, md, partition, user = disp.updated[0]
    assert file_id == "f1"
    assert md == {"author": "bob", "file_id": "f1"}
    assert partition == "p1"
    assert user == {"id": 1}


@pytest.mark.asyncio
async def test_copy_file_sets_target_fields():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-dst",
        metadata={"k": "v"},
        user={"id": 2},
    )
    file_id, md, partition, user = disp.copied[0]
    assert file_id == "src"
    assert partition == "p-src"
    assert md == {"k": "v", "file_id": "dst", "partition": "p-dst"}
    assert user == {"id": 2}


@pytest.mark.asyncio
async def test_task_state_and_error_passthrough():
    svc = _service()
    assert await svc.get_task_state("t1") == "QUEUED"
    assert await svc.get_task_error("t1") == "trace"


@pytest.mark.asyncio
async def test_cancel_task_passthrough():
    disp = FakeDispatcher()
    disp.cancel_result = False
    svc = _service(disp=disp)
    assert await svc.cancel_task("t1") is False
    assert disp.cancelled == ["t1"]
