"""Unit tests for :class:`IndexingService` (Phase 8D.1)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.utils.exceptions import AuthError, ConfigError, PartitionNotFoundError, ValidationError
from services.orchestrators.indexing_service import IndexingService


class FakeDocumentRepo:
    def __init__(self, *, exists: bool = False, raise_on_check: bool = False, content_sha256: str | None = None):
        self._exists = exists
        self._raise = raise_on_check
        self._content_sha256 = content_sha256

    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool:
        if self._raise:
            raise RuntimeError("boom")
        return self._exists

    async def get_content_sha256(self, file_id: str, partition: str) -> str | None:
        return self._content_sha256


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
        callback_url=None,
        callback_token=None,
        require_existing_partition=False,
        allow_legacy_require_existing_partition_retry=False,
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
                "callback_url": callback_url,
                "callback_token": callback_token,
                "require_existing_partition": require_existing_partition,
                "allow_legacy_require_existing_partition_retry": allow_legacy_require_existing_partition_retry,
            }
        )
        return "task-abc"

    async def delete_file(self, file_id, partition):
        self.deleted.append((file_id, partition))

    async def update_file_metadata(self, file_id, metadata, partition, user):
        self.updated.append((file_id, metadata, partition, user))

    async def copy_file(self, file_id, metadata, partition, user, **destination):
        self.copied.append((file_id, metadata, partition, user))
        self.copy_destination = destination

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
        self.admissions: list[str] = []
        self.admission_depth = 0
        self.copy_locks: list[tuple[str, int]] = []
        self.copying = 0

    def _cfg(self, partition: str) -> PartitionConfig:
        return PartitionConfig(
            name=partition,
            embedder="default",
            indexation=IndexationPipelineConfig(),
            retrieval=RetrievalPipelineConfig(),
        )

    async def partition_exists(self, partition: str) -> bool:
        return partition in self._db

    @asynccontextmanager
    async def indexing_admission(self, partition: str):
        self.admissions.append(partition)
        self.admission_depth += 1
        try:
            yield await self.partition_exists(partition)
        finally:
            self.admission_depth -= 1

    @asynccontextmanager
    async def copy_in_flight(self, partition: str):
        self.copy_locks.append((partition, self.admission_depth))
        self.copying += 1
        try:
            yield
        finally:
            self.copying -= 1

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


class _PinningPartitionService(FakePartitionService):
    """Pins a partition off the `default` alias the way the real service does."""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.pins: list[tuple[str, int]] = []

    async def pin_embedder_for_write(self, partition: str) -> None:
        self.pins.append((partition, self.admission_depth))
        cfg = self._config.partitions.get(partition)
        if cfg is not None and cfg.embedder == "default":
            self._config.partitions[partition] = cfg.model_copy(update={"embedder": "jina"})


class _RefreshingPresetService:
    def __init__(self, config) -> None:
        self.calls = 0
        self._config = config

    async def refresh_if_stale(self) -> bool:
        self.calls += 1
        self._config.partitions["tenant-a"] = PartitionConfig(
            name="tenant-a",
            embedder="embed-fast",
            indexation=IndexationPipelineConfig(asr_transcription_prompt_name="meeting-notes-v2"),
            retrieval=RetrievalPipelineConfig(),
        )
        return True


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


def _service(*, doc=None, ws=None, disp=None, config=None, partition_service=None, preset_service=None):
    kwargs = {
        "document_repo": doc or FakeDocumentRepo(),
        "workspace_repo": ws or FakeWorkspaceRepo(),
        "dispatcher": disp or FakeDispatcher(),
        "config": config,
        "partition_service": partition_service,
    }
    if preset_service is not None:
        kwargs["preset_service"] = preset_service
    return IndexingService(**kwargs)


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
    assert sent["require_existing_partition"] is False
    md = sent["metadata"]
    assert md["author"] == "alice"
    assert md["source"] == str(f)
    assert md["filename"] == "doc.txt"
    assert md["original_filename"] == "Doc Original.txt"
    assert md["file_id"] == "f1"
    assert md["file_size"] == "11.00 B"
    assert md["content_sha256"] is None


@pytest.mark.asyncio
async def test_add_file_drops_caller_supplied_degraded_stages(tmp_path):
    file_path = tmp_path / "doc.txt"
    file_path.write_text("hello world")
    dispatcher = FakeDispatcher()
    service = _service(disp=dispatcher)

    await service.add_file(
        file_path=str(file_path),
        file_id="f1",
        partition="p1",
        metadata={"author": "alice", "degraded_stages": ["caption"]},
        sanitized_filename="doc.txt",
        original_filename=None,
        user={"id": 7},
    )

    assert dispatcher.dispatched[0]["metadata"].get("degraded_stages") is None


@pytest.mark.asyncio
async def test_add_file_uses_precomputed_content_hash_when_deduplication_is_enabled(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world")
    disp = FakeDispatcher()
    config = SimpleNamespace(loader=SimpleNamespace(content_deduplication_enabled=True), partitions={})
    svc = _service(disp=disp, config=config)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
        content_sha256="abc123",
    )

    assert disp.dispatched[0]["metadata"]["content_sha256"] == "abc123"


@pytest.mark.asyncio
async def test_add_file_hashes_non_http_input_when_digest_is_not_precomputed(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world")
    disp = FakeDispatcher()
    config = SimpleNamespace(loader=SimpleNamespace(content_deduplication_enabled=True), partitions={})
    svc = _service(disp=disp, config=config)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
    )

    assert (
        disp.dispatched[0]["metadata"]["content_sha256"]
        == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


@pytest.mark.asyncio
async def test_add_file_holds_partition_admission_fence_until_dispatch(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    config = type("Config", (), {"partitions": {}})()
    psvc = FakePartitionService(config, db_partitions={"tenant-a"})

    class CheckingDispatcher(FakeDispatcher):
        async def dispatch_indexing(self, **kwargs):
            assert psvc.admission_depth == 1
            return await super().dispatch_indexing(**kwargs)

    disp = CheckingDispatcher()
    svc = _service(disp=disp, config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
    )

    assert psvc.admissions == ["tenant-a"]
    assert psvc.admission_depth == 0


@pytest.mark.asyncio
async def test_add_file_requires_existing_partition_when_configless_partition_existed_at_admission(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = type("Config", (), {"partitions": {}})()
    psvc = FakePartitionService(config, db_partitions={"tenant-a"})
    svc = _service(disp=disp, config=config, partition_service=psvc)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user={"id": 7},
    )

    assert psvc.created == []
    assert disp.dispatched[0]["require_existing_partition"] is True
    assert disp.dispatched[0]["allow_legacy_require_existing_partition_retry"] is False


@pytest.mark.asyncio
async def test_add_file_keeps_legacy_auto_create_for_new_configless_partition(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = type("Config", (), {"partitions": {}})()
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

    assert psvc.created == []
    assert disp.dispatched[0]["require_existing_partition"] is False
    assert disp.dispatched[0]["allow_legacy_require_existing_partition_retry"] is False


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
    assert sent["require_existing_partition"] is True
    assert sent["allow_legacy_require_existing_partition_retry"] is True


@pytest.mark.asyncio
async def test_add_file_pins_the_partition_and_dispatches_the_pinned_embedder(tmp_path):
    """A partition on the `default` alias is pinned under the admission fence,
    and the worker is handed the endpoint name — never `default`, which it
    would resolve on its own, later, after the default may have moved (#762)."""
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    config = SimpleNamespace(partitions={})
    partition_service = _PinningPartitionService(config, db_partitions={"tenant-a"})
    config.partitions["tenant-a"] = partition_service._cfg("tenant-a")
    svc = _service(disp=disp, config=config, partition_service=partition_service)

    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
    )

    assert partition_service.pins == [("tenant-a", 1)]
    assert disp.dispatched[0]["embedder_name"] == "jina"


@pytest.mark.asyncio
async def test_copy_file_pins_the_target_partition_before_dispatch():
    disp = FakeDispatcher()
    config = SimpleNamespace(partitions={})
    partition_service = _PinningPartitionService(config)
    config.partitions["p-dst"] = partition_service._cfg("p-dst")
    svc = _service(disp=disp, config=config, partition_service=partition_service)

    await svc.copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-dst",
        metadata={},
        user={"id": 2},
    )

    assert [p for p, _ in partition_service.pins] == ["p-dst"]
    assert config.partitions["p-dst"].embedder == "jina"
    assert disp.copied


@pytest.mark.asyncio
async def test_add_file_refreshes_stale_preset_configuration_before_dispatch(tmp_path):
    file_path = tmp_path / "audio.mp3"
    file_path.write_text("x")
    config = _config_with_partition("tenant-a")
    partition_service = FakePartitionService(config, db_partitions={"tenant-a"})
    preset_service = _RefreshingPresetService(config)
    dispatcher = FakeDispatcher()
    service = _service(
        disp=dispatcher,
        config=config,
        partition_service=partition_service,
        preset_service=preset_service,
    )

    await service.add_file(
        file_path=str(file_path),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="audio.mp3",
        original_filename="audio.mp3",
        user=None,
    )

    assert preset_service.calls == 1
    assert partition_service.loaded == 0
    assert dispatcher.dispatched[0]["indexation_config"]["asr_transcription_prompt_name"] == "meeting-notes-v2"


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

    with pytest.raises(AuthError, match="Editor role required") as exc_info:
        await svc.add_file(
            file_path=str(f),
            file_id="f1",
            partition="tenant-new",
            metadata={},
            sanitized_filename="doc.txt",
            original_filename="doc.txt",
            user={"id": 7},
        )

    # Maps to a clean 403 Forbidden, not the catch-all 500.
    assert exc_info.value.status_code == 403
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
async def test_update_metadata_cannot_override_content_hash():
    disp = FakeDispatcher()
    svc = _service(disp=disp)

    await svc.update_metadata(
        "f1",
        {"content_sha256": "client-controlled", "author": "bob"},
        "p1",
        {"id": 1},
    )

    assert disp.updated[0][1] == {"author": "bob", "file_id": "f1"}


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
    assert md == {"k": "v", "file_id": "dst", "partition": "p-dst", "content_sha256": None}
    assert user == {"id": 2}


@pytest.mark.asyncio
async def test_copy_file_preserves_content_hash_when_deduplication_is_enabled():
    disp = FakeDispatcher()
    config = SimpleNamespace(loader=SimpleNamespace(content_deduplication_enabled=True), partitions={})
    svc = _service(doc=FakeDocumentRepo(content_sha256="abc123"), disp=disp, config=config)

    await svc.copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-dst",
        metadata={},
        user={"id": 2},
    )

    assert disp.copied[0][1]["content_sha256"] == "abc123"


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


# --- #713: server-managed metadata keys must never come from the caller ----


@pytest.mark.asyncio
async def test_update_metadata_drops_protected_keys():
    """A partition editor must not be able to repoint ``source``.

    ``source`` is the filesystem path served by ``GET /static/{extract_id}``,
    which authorizes on the chunk's partition — unchanged by this write. Left
    unfiltered, an editor could aim it at another tenant's upload in the shared
    data dir and read it back.
    """
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.update_metadata(
        "f1",
        {
            "author": "bob",
            "source": "/app/data/other_tenant_secret.pdf",
            "created_by": 999,
            "file_size": 1,
            "vector": [0.1],
            "text": "spoofed",
            "_id": "x",
            "file_count": 42,
            "indexed_at": "2099-01-01T00:00:00+00:00",
            "degraded_stages": ["caption"],
        },
        "p1",
        {"id": 1},
    )
    _, md, _, _ = disp.updated[0]
    assert md == {"author": "bob", "file_id": "f1"}
    for key in (
        "source",
        "created_by",
        "file_size",
        "vector",
        "text",
        "_id",
        "file_count",
        "indexed_at",
        "degraded_stages",
    ):
        assert key not in md


@pytest.mark.asyncio
async def test_copy_file_drops_protected_keys():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.copy_file(
        source_file_id="src",
        source_partition="p1",
        target_file_id="dst",
        target_partition="p2",
        metadata={
            "author": "bob",
            "source": "/app/data/other_tenant_secret.pdf",
            "degraded_stages": ["caption"],
        },
        user={"id": 1},
    )
    _, md, _, _ = disp.copied[0]
    assert "source" not in md
    assert "degraded_stages" not in md
    assert md["author"] == "bob"
    assert md["file_id"] == "dst"
    assert md["partition"] == "p2"


def test_build_metadata_only_adds_keys_in_upload_metadata_server_keys(tmp_path):
    """The indexing-status callback echoes this dict's caller-supplied fields to
    a caller-chosen URL, excluding exactly UPLOAD_METADATA_SERVER_KEYS. If this
    method ever starts injecting a new server-computed key without adding it to
    that constant too, the new key leaks into every callback going forward —
    this test is the safety net for that drift."""
    from core.utils.consts import UPLOAD_METADATA_SERVER_KEYS

    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"content")

    svc = _service()
    caller_metadata = {"doc_rev": "3-abc", "app_tag": "x"}
    full = svc._build_metadata(
        metadata=caller_metadata,
        file_path=str(file_path),
        file_id="file-1",
        sanitized_filename="doc.pdf",
        original_filename="Original.pdf",
        content_sha256="9f86d081",
    )

    injected_keys = set(full) - set(caller_metadata)
    assert injected_keys <= UPLOAD_METADATA_SERVER_KEYS, (
        f"_build_metadata injected {injected_keys - UPLOAD_METADATA_SERVER_KEYS}, "
        "not covered by UPLOAD_METADATA_SERVER_KEYS — the indexing-status "
        "callback would now leak it to a caller-supplied URL"
    )
    assert full["doc_rev"] == "3-abc"
    assert full["app_tag"] == "x"


class _ExplodingPresetService:
    """PresetService whose revision probe fails the way a DB blip does.

    ``PgPresetRepository.latest_revision`` raises outright when the
    ``preset_configuration_revision`` row is missing, and asyncpg surfaces any
    transient connection fault the same way.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def refresh_if_stale(self) -> bool:
        self.calls += 1
        raise RuntimeError("Preset configuration revision row is missing.")


@pytest.mark.asyncio
async def test_add_file_survives_a_failing_preset_staleness_probe(tmp_path):
    """A failed revision probe must not fail the upload.

    Regression: the probe runs inside ``add_file``'s admission block, so leaving
    it unguarded turned any transient DB fault — or a missing revision row — into
    a 500 on *every* upload, of every file type, in every partition. It is an
    optimization for noticing another replica's preset writes, never a
    precondition for accepting a file, so a failure falls through to the cached
    config and the dispatch still happens.
    """
    file_path = tmp_path / "doc.txt"
    file_path.write_text("x")
    config = _config_with_partition("tenant-a")
    partition_service = FakePartitionService(config, db_partitions={"tenant-a"})
    preset_service = _ExplodingPresetService()
    dispatcher = FakeDispatcher()
    service = _service(
        disp=dispatcher,
        config=config,
        partition_service=partition_service,
        preset_service=preset_service,
    )

    await service.add_file(
        file_path=str(file_path),
        file_id="f1",
        partition="tenant-a",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
    )

    assert preset_service.calls == 1
    assert len(dispatcher.dispatched) == 1


# ---------------------------------------------------------------------------
# Copy into a partition on another embedder
# ---------------------------------------------------------------------------


def _copy_config(embedders: dict) -> SimpleNamespace:
    from core.config.model_endpoints import ModelEndpointConfig

    return SimpleNamespace(
        partitions={
            "p-dst": PartitionConfig(
                name="p-dst",
                embedder="bge-m3",
                indexation=IndexationPipelineConfig(),
                retrieval=RetrievalPipelineConfig(),
            )
        },
        models=SimpleNamespace(
            embedder={
                name: ModelEndpointConfig(endpoint="http://bge:8000/v1", vector_field=field)
                for name, field in embedders.items()
            }
        ),
        loader=SimpleNamespace(content_deduplication_enabled=False),
    )


def _copy_destination(vector_field: str, embedder: str) -> dict:
    from core.config.model_endpoints import embedder_fingerprint

    return {
        "vector_field": vector_field,
        "embedder": f"embedder:{embedder}",
        "embedder_reference": embedder,
        "embedder_fingerprint": embedder_fingerprint("http://bge:8000/v1", None, {}),
    }


def _copy_service(config: SimpleNamespace, disp: FakeDispatcher, partition_service=None) -> IndexingService:
    return IndexingService(
        document_repo=FakeDocumentRepo(),
        workspace_repo=FakeWorkspaceRepo(),
        dispatcher=disp,
        config=config,
        partition_service=partition_service,
        embedder_factory=lambda name: f"embedder:{name}",
    )


async def _copy_into_p_dst(svc: IndexingService) -> None:
    await svc.copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-dst",
        metadata={},
        user=None,
    )


@pytest.mark.asyncio
async def test_a_copy_lands_in_the_target_partitions_embedder_field():
    disp = FakeDispatcher()

    await _copy_into_p_dst(_copy_service(_copy_config({"bge-m3": "vector_bge_m3"}), disp))

    assert disp.copy_destination == _copy_destination("vector_bge_m3", "bge-m3")


@pytest.mark.asyncio
@pytest.mark.parametrize("embedders", [{}, {"bge-m3": None}], ids=["unknown embedder", "no vector field"])
async def test_a_copy_that_cannot_be_routed_is_refused(embedders):
    # Written to any other field, the copy would never be found.
    disp = FakeDispatcher()

    with pytest.raises(ConfigError, match="no vector field to go to"):
        await _copy_into_p_dst(_copy_service(_copy_config(embedders), disp))

    assert disp.copied == []


@pytest.mark.asyncio
async def test_a_copy_into_a_missing_partition_creates_it_like_an_upload():
    config = _copy_config({"jina": "vector_jina"})
    partition_service = _PinningPartitionService(config, db_partitions={"p-dst"})
    disp = FakeDispatcher()

    await _copy_service(config, disp, partition_service).copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-new",
        metadata={},
        user={"id": 2},
    )

    assert partition_service.created == [("p-new", 2)]
    # Created on the `default` alias, then pinned to the embedder it resolves to.
    assert disp.copy_destination == _copy_destination("vector_jina", "jina")


@pytest.mark.asyncio
async def test_a_copy_is_admitted_under_the_fence_but_does_not_hold_it():
    # Uploads to the partition wait on the fence, and a copy can re-embed for
    # minutes. An embedder change sees the copy lock instead, taken under the fence.
    config = _copy_config({"bge-m3": "vector_bge_m3"})
    partition_service = FakePartitionService(config, db_partitions={"p-dst"})
    seen = []

    class _Dispatcher(FakeDispatcher):
        async def copy_file(self, *args, **kwargs):
            seen.append((partition_service.admission_depth, partition_service.copying))

    await _copy_into_p_dst(_copy_service(config, _Dispatcher(), partition_service))

    assert partition_service.admissions == ["p-dst"]
    assert partition_service.copy_locks == [("p-dst", 1)]
    assert seen == [(0, 1)]
    assert partition_service.copying == 0
