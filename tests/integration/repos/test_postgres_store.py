"""Phase 7F — PostgresStore composite against a real Postgres."""

from __future__ import annotations

import pytest
from core.config.infrastructure import RDBConfig
from core.ports.catalog_store import CatalogStore
from services.persistence.audit_log_repo import PgAuditLogRepository
from services.persistence.chunk_repo import PgChunkRepository
from services.persistence.conversation_repo import PgConversationRepository
from services.persistence.document_repo import PgDocumentRepository
from services.persistence.entity_repo import PgEntityRepository
from services.persistence.idempotency_repo import PgIdempotencyRepository
from services.persistence.job_repo import PgJobRepository
from services.persistence.model_endpoint_repo import PgModelEndpointRepository
from services.persistence.oidc_session_repo import PgOIDCSessionRepository
from services.persistence.partition_repo import PgPartitionRepository
from services.persistence.preset_repo import PgPresetRepository
from services.persistence.prompt_repo import PgPromptRepository
from services.persistence.topic_tag_repo import PgTopicTagRepository
from services.persistence.user_repo import PgUserRepository
from services.persistence.workspace_repo import PgWorkspaceRepository
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


_EXPECTED_REPO_TYPES = {
    "document_repo": PgDocumentRepository,
    "user_repo": PgUserRepository,
    "partition_repo": PgPartitionRepository,
    "oidc_session_repo": PgOIDCSessionRepository,
    "workspace_repo": PgWorkspaceRepository,
    "job_repo": PgJobRepository,
    "chunk_repo": PgChunkRepository,
    "prompt_repo": PgPromptRepository,
    "conversation_repo": PgConversationRepository,
    "audit_log_repo": PgAuditLogRepository,
    "idempotency_repo": PgIdempotencyRepository,
    "entity_repo": PgEntityRepository,
    "topic_tag_repo": PgTopicTagRepository,
    "model_endpoint_repo": PgModelEndpointRepository,
    "preset_repo": PgPresetRepository,
}


class TestComposite:
    async def test_satisfies_catalog_store_abc(self, postgres_store: PostgresStore):
        assert isinstance(postgres_store, CatalogStore)

    async def test_pool_open_after_initialize(self, postgres_store: PostgresStore):
        # ``pool`` raises if initialize() never ran. The session-scoped
        # fixture initialises so this must succeed.
        assert postgres_store.pool is not None

    @pytest.mark.parametrize("name", sorted(_EXPECTED_REPO_TYPES))
    async def test_all_fifteen_repos_exposed(
        self,
        postgres_store: PostgresStore,
        name: str,
    ):
        repo = getattr(postgres_store, name)
        assert isinstance(repo, _EXPECTED_REPO_TYPES[name])

    async def test_repo_properties_are_idempotent(self, postgres_store: PostgresStore):
        # Each property must return the exact same instance every call —
        # orchestrators cache repos and rely on identity.
        first = postgres_store.document_repo
        assert postgres_store.document_repo is first


class TestMigrationIdempotency:
    async def test_run_migrations_twice_is_safe(self, postgres_store: PostgresStore):
        # The session fixture already ran migrations once. Re-running must
        # be a no-op — every phase-7 Alembic revision is supposed to guard
        # its DDL with an inspector check (see CLAUDE.md "Alembic Migration
        # Idempotency").
        await postgres_store._conn.run_migrations()  # noqa: SLF001


class TestLifecycle:
    """Initialise/shutdown a fresh store without disturbing the session pool."""

    async def test_initialize_then_shutdown(self, test_rdb_config: RDBConfig):
        # The session store already migrated the DB so we can skip migrations
        # here and just exercise the pool lifecycle.
        store = PostgresStore(test_rdb_config, run_migrations=False)
        await store.initialize()
        try:
            assert store.pool is not None
        finally:
            await store.shutdown()
        with pytest.raises(RuntimeError, match="initialize"):
            _ = store.pool

    async def test_initialize_is_idempotent_no_remigrate(self, test_rdb_config: RDBConfig):
        # ``get_vectordb()`` re-invokes the actor's initialize on every
        # request (it is a FastAPI ``Depends``). Re-running must NOT re-run
        # Alembic — a per-request migration storm starves the PG pool and
        # surfaces as 500s under load (regression for the api-tests flake).
        store = PostgresStore(test_rdb_config)
        calls = 0
        original = store._conn.run_migrations  # noqa: SLF001

        async def counting_run_migrations() -> None:
            nonlocal calls
            calls += 1
            await original()

        store._conn.run_migrations = counting_run_migrations  # type: ignore[method-assign]  # noqa: SLF001
        try:
            await store.initialize()
            await store.initialize()
            await store.initialize()
            assert calls == 1
        finally:
            await store.shutdown()
