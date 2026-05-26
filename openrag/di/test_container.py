"""Phase 7E — ServiceContainer storage wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.infrastructure import RDBConfig, VectorDBConfig
from core.config.root import Settings
from core.ports.audit_log_repo import AuditLogRepository
from core.ports.catalog_store import CatalogStore
from core.ports.chunk_repo import ChunkRepository
from core.ports.conversation_repo import ConversationRepository
from core.ports.document_repo import DocumentRepository
from core.ports.entity_repo import EntityRepository
from core.ports.idempotency_repo import IdempotencyRepository
from core.ports.job_repo import JobRepository
from core.ports.model_endpoint_repo import ModelEndpointRepository
from core.ports.oidc_session_repo import OIDCSessionRepository
from core.ports.partition_repo import PartitionRepository
from core.ports.preset_repo import PresetRepository
from core.ports.prompt_repo import PromptRepository
from core.ports.topic_tag_repo import TopicTagRepository
from core.ports.user_repo import UserRepository
from core.ports.workspace_repo import WorkspaceRepository
from di.container import ServiceContainer
from di.repositories import create_catalog_store
from di.vector_stores import create_vector_store


def _settings(database: str | None = None, collection: str = "vdb_test") -> Settings:
    return Settings(
        rdb=RDBConfig(password="x", database=database),
        vectordb=VectorDBConfig(collection_name=collection),
    )


@pytest.fixture(autouse=True)
def _stub_milvus_clients(monkeypatch):
    """Replace the pymilvus gRPC clients with mocks for the whole module.

    ``ServiceContainer(settings)`` eagerly builds a :class:`MilvusVectorStore`
    in its constructor; the real pymilvus client tries to open a channel
    immediately and hangs if Milvus is unreachable. These tests only need
    to verify the *wiring*, so we stub the clients out. Real Milvus
    coverage lives in ``tests/integration/test_milvus_store_integration.py``.
    """
    from unittest.mock import MagicMock

    import services.storage.milvus_store as ms

    monkeypatch.setattr(ms, "MilvusClient", MagicMock())
    monkeypatch.setattr(ms, "AsyncMilvusClient", MagicMock())


class TestLegacyContainerStillWorks:
    """The pre-Phase-7E callers do ``ServiceContainer()`` with no settings."""

    def test_constructs_without_settings(self):
        ServiceContainer()  # must not raise

    def test_catalog_store_raises_when_unconfigured(self):
        c = ServiceContainer()
        with pytest.raises(RuntimeError, match="without a Settings instance"):
            _ = c.catalog_store

    def test_vector_store_raises_when_unconfigured(self):
        c = ServiceContainer()
        with pytest.raises(RuntimeError, match="without a Settings instance"):
            _ = c.vector_store

    @pytest.mark.parametrize(
        "name",
        [
            "document_repo",
            "user_repo",
            "partition_repo",
            "oidc_session_repo",
            "workspace_repo",
            "job_repo",
            "chunk_repo",
            "prompt_repo",
            "conversation_repo",
            "audit_log_repo",
            "idempotency_repo",
            "entity_repo",
            "topic_tag_repo",
            "model_endpoint_repo",
            "preset_repo",
        ],
    )
    def test_repo_properties_raise_when_unconfigured(self, name):
        c = ServiceContainer()
        with pytest.raises(RuntimeError, match="without a Settings instance"):
            getattr(c, name)


class TestCatalogStoreWiring:
    def test_catalog_store_satisfies_port(self):
        c = ServiceContainer(_settings())
        assert isinstance(c.catalog_store, CatalogStore)

    def test_database_name_derived_from_collection(self):
        c = ServiceContainer(_settings(database=None, collection="my_collection"))
        assert c.catalog_store._conn._conn_kwargs["database"] == "partitions_for_collection_my_collection"

    def test_explicit_database_overrides_fallback(self):
        c = ServiceContainer(_settings(database="custom_db", collection="my_collection"))
        assert c.catalog_store._conn._conn_kwargs["database"] == "custom_db"

    @pytest.mark.parametrize(
        ("name", "port"),
        [
            ("document_repo", DocumentRepository),
            ("user_repo", UserRepository),
            ("partition_repo", PartitionRepository),
            ("oidc_session_repo", OIDCSessionRepository),
            ("workspace_repo", WorkspaceRepository),
            ("job_repo", JobRepository),
            ("chunk_repo", ChunkRepository),
            ("prompt_repo", PromptRepository),
            ("conversation_repo", ConversationRepository),
            ("audit_log_repo", AuditLogRepository),
            ("idempotency_repo", IdempotencyRepository),
            ("entity_repo", EntityRepository),
            ("topic_tag_repo", TopicTagRepository),
            ("model_endpoint_repo", ModelEndpointRepository),
            ("preset_repo", PresetRepository),
        ],
    )
    def test_repo_property_returns_port_typed_instance(self, name, port):
        c = ServiceContainer(_settings())
        repo = getattr(c, name)
        assert isinstance(repo, port)
        # Container shortcuts must be the same object exposed by the store —
        # otherwise orchestrator injection drifts from store state.
        assert repo is getattr(c.catalog_store, name)

    @pytest.mark.asyncio
    async def test_initialize_seeds_admin_token(self, monkeypatch):
        calls = []

        async def ensure_admin_user(token):
            calls.append(token)

        class FakeCatalogStore:
            user_repo = SimpleNamespace(ensure_admin_user=ensure_admin_user)

            async def initialize(self):
                calls.append("initialize")

        monkeypatch.setenv("AUTH_TOKEN", "admin-token")
        c = ServiceContainer(_settings())
        c._catalog_store = FakeCatalogStore()

        await c.initialize()

        assert calls == ["initialize", "admin-token"]


class TestVectorStoreWiring:
    """The factory returns a real :class:`MilvusVectorStore`; pymilvus gRPC
    clients are patched out so these unit tests don't need Milvus reachable.
    The full integration coverage lives in
    ``tests/integration/test_milvus_store_integration.py``."""

    def test_factory_returns_milvus_vector_store(self):
        from services.storage.milvus_store import MilvusVectorStore

        store = create_vector_store(_settings())
        assert isinstance(store, MilvusVectorStore)

    def test_container_property_returns_milvus_vector_store(self):
        from services.storage.milvus_store import MilvusVectorStore

        c = ServiceContainer(_settings())
        assert isinstance(c.vector_store, MilvusVectorStore)

    def test_container_caches_vector_store(self):
        c = ServiceContainer(_settings())
        # Repeated property reads must return the same instance — every
        # construction opens a fresh pymilvus gRPC channel.
        assert c.vector_store is c.vector_store


class TestRepositoriesFactory:
    def test_returns_a_catalog_store(self):
        assert isinstance(create_catalog_store(_settings()), CatalogStore)

    def test_run_migrations_flag_propagates(self):
        store = create_catalog_store(_settings(), run_migrations=False)
        assert store._run_migrations is False

    def test_does_not_mutate_input_settings(self):
        s = _settings(database=None, collection="abc")
        original_database = s.rdb.database
        create_catalog_store(s)
        # The factory uses model_copy with an update — the source must stay None.
        assert s.rdb.database == original_database


# Phase 8F — every orchestrator is a lazy cached property on the
# container and is reachable through a one-liner provider. Keyed by the
# container property name; value is the matching provider function.
_ORCHESTRATORS = [
    ("auth_service", "get_auth_service"),
    ("user_service", "get_user_service"),
    ("partition_service", "get_partition_service"),
    ("workspace_service", "get_workspace_service"),
    ("retrieval_service", "get_retrieval_service"),
    ("query_service", "get_query_service"),
    ("indexing_service", "get_indexing_service"),
    ("job_service", "get_job_service"),
    ("conversion_service", "get_conversion_service"),
]


class TestPhase8OrchestratorWiring:
    """8F: all nine orchestrators wired consistently (container + providers)."""

    @pytest.mark.parametrize("prop,_provider", _ORCHESTRATORS)
    def test_property_is_lazy_and_cache_slot_starts_none(self, prop, _provider):
        # The public accessor is a property (lazy), not an eager attribute.
        assert isinstance(getattr(ServiceContainer, prop), property)
        # The cache slot exists and is None before first access (no
        # settings needed — the legacy no-arg path must keep working).
        assert getattr(ServiceContainer(), f"_{prop}") is None

    @pytest.mark.parametrize("prop,provider", _ORCHESTRATORS)
    def test_provider_delegates_to_container_property(self, prop, provider):
        from types import SimpleNamespace

        from di import providers

        sentinel = object()
        fake_container = SimpleNamespace(**{prop: sentinel})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=fake_container)))

        resolved = getattr(providers, provider)(request)
        assert resolved is sentinel

    def test_no_orchestrator_is_missing_a_provider(self):
        from di import providers

        wired = {name for name in vars(providers) if name.startswith("get_") and name.endswith("_service")}
        assert wired == {p for _, p in _ORCHESTRATORS}
