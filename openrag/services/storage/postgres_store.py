"""PostgresStore — concrete :class:`~openrag.core.ports.catalog_store.CatalogStore`.

Composes the connection manager (Phase 7A.1) with every repository
implementation (Phase 7A.2) into a single aggregate root that orchestrators
and the Phase 7C shim consume through the ``CatalogStore`` ABC.

The store owns the lifecycle:

* :meth:`initialize` opens the asyncpg pool, then optionally runs Alembic
  migrations to ``head``. Migrations stay idempotent so they can be run either
  during app startup or as a separate deployment step.
* :meth:`shutdown` closes the pool. Repositories share the pool via a
  ``pool_getter`` callable, so the pool can be reinitialised in tests without
  rebuilding the repos.

The optional :pyattr:`pool` property is an escape hatch for cross-repo
transactional work — Phase 8 orchestrators will reach for it to wrap multiple
repo writes in a single :func:`asyncpg.Pool.acquire` + ``conn.transaction()``
context. It is not part of the ABC contract; clients that only need a single
repository call should never touch it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ports.catalog_store import CatalogStore
from services.persistence.audit_log_repo import PgAuditLogRepository
from services.persistence.chunk_repo import PgChunkRepository
from services.persistence.connection import ConnectionManager
from services.persistence.conversation_repo import PgConversationRepository
from services.persistence.document_repo import PgDocumentRepository
from services.persistence.entity_repo import PgEntityRepository
from services.persistence.evaluation_repo import PgEvaluationRepository
from services.persistence.idempotency_repo import PgIdempotencyRepository
from services.persistence.job_repo import PgJobRepository
from services.persistence.model_endpoint_repo import PgModelEndpointRepository
from services.persistence.oidc_session_repo import PgOIDCSessionRepository
from services.persistence.partition_membership_repo import PgPartitionMembershipRepository
from services.persistence.partition_repo import PgPartitionRepository
from services.persistence.preset_repo import PgPresetRepository
from services.persistence.prompt_repo import PgPromptRepository
from services.persistence.topic_tag_repo import PgTopicTagRepository
from services.persistence.user_repo import PgUserRepository
from services.persistence.workspace_repo import PgWorkspaceRepository

if TYPE_CHECKING:
    import asyncpg
    from core.config.infrastructure import RDBConfig
    from core.ports.audit_log_repo import AuditLogRepository
    from core.ports.chunk_repo import ChunkRepository
    from core.ports.conversation_repo import ConversationRepository
    from core.ports.document_repo import DocumentRepository
    from core.ports.entity_repo import EntityRepository
    from core.ports.evaluation_repo import EvaluationRepository
    from core.ports.idempotency_repo import IdempotencyRepository
    from core.ports.job_repo import JobRepository
    from core.ports.model_endpoint_repo import ModelEndpointRepository
    from core.ports.oidc_session_repo import OIDCSessionRepository
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.partition_repo import PartitionRepository
    from core.ports.preset_repo import PresetRepository
    from core.ports.prompt_repo import PromptRepository
    from core.ports.topic_tag_repo import TopicTagRepository
    from core.ports.user_repo import UserRepository
    from core.ports.workspace_repo import WorkspaceRepository


class PostgresStore(CatalogStore):
    """asyncpg-backed :class:`CatalogStore` composing all repository ports."""

    def __init__(self, config: RDBConfig, *, run_migrations: bool = True) -> None:
        self._conn = ConnectionManager(config)
        self._run_migrations = run_migrations
        self._initialized = False

        # Repositories take a pool_getter callable instead of a pool reference
        # so they always see the live pool even if ConnectionManager is
        # reinitialised between tests.
        pool_getter = self._pool_getter

        self._document_repo = PgDocumentRepository(pool_getter)
        self._user_repo = PgUserRepository(pool_getter)
        self._partition_repo = PgPartitionRepository(pool_getter)
        self._membership_repo = PgPartitionMembershipRepository(pool_getter)
        self._oidc_session_repo = PgOIDCSessionRepository(pool_getter)
        self._workspace_repo = PgWorkspaceRepository(pool_getter)

        # Stubs — every method raises StubRepositoryError until the matching
        # table exists. Listed in the post-refactoring roadmap.
        self._job_repo = PgJobRepository(pool_getter)
        self._chunk_repo = PgChunkRepository(pool_getter)
        self._prompt_repo = PgPromptRepository(pool_getter)
        self._conversation_repo = PgConversationRepository(pool_getter)
        self._audit_log_repo = PgAuditLogRepository(pool_getter)
        self._idempotency_repo = PgIdempotencyRepository(pool_getter)
        self._entity_repo = PgEntityRepository(pool_getter)
        self._topic_tag_repo = PgTopicTagRepository(pool_getter)
        self._model_endpoint_repo = PgModelEndpointRepository(pool_getter)
        self._preset_repo = PgPresetRepository(pool_getter)
        self._evaluation_repo = PgEvaluationRepository(pool_getter)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the asyncpg pool, then upgrade the schema to ``head``.

        Idempotent: ``get_vectordb()`` re-invokes the actor's ``initialize``
        on every request (it is a FastAPI ``Depends``), and the documented
        contract is that re-running on a hot actor is a no-op. Without the
        ``_initialized`` guard each request would re-run a full Alembic
        ``command.upgrade``, which under concurrent load starves the Postgres
        pool and surfaces as 500s.

        Migrations are still idempotent because the same revision can be
        applied from app startup in local/dev or from a deployment job in
        managed environments.
        """
        if self._initialized:
            return
        await self._conn.initialize()
        if self._run_migrations:
            await self._conn.run_migrations()
        self._initialized = True

    async def shutdown(self) -> None:
        await self._conn.shutdown()
        self._initialized = False

    # ------------------------------------------------------------------
    # Connection access (escape hatch for Phase 8 orchestrators)
    # ------------------------------------------------------------------

    @property
    def pool(self) -> asyncpg.Pool:
        """Raw asyncpg pool for cross-repo transactional work.

        Phase 8 orchestrators need to wrap multi-repo writes in a single
        transaction (e.g. delete-document + delete-chunks). This property
        exposes the pool *only* to that caller — most code paths should
        never touch it.
        """
        return self._conn.pool

    # ------------------------------------------------------------------
    # Real repos
    # ------------------------------------------------------------------

    @property
    def document_repo(self) -> DocumentRepository:
        return self._document_repo

    @property
    def user_repo(self) -> UserRepository:
        return self._user_repo

    @property
    def partition_repo(self) -> PartitionRepository:
        return self._partition_repo

    @property
    def membership_repo(self) -> PartitionMembershipRepository:
        return self._membership_repo

    @property
    def oidc_session_repo(self) -> OIDCSessionRepository:
        return self._oidc_session_repo

    @property
    def workspace_repo(self) -> WorkspaceRepository:
        return self._workspace_repo

    # ------------------------------------------------------------------
    # Stub repos — methods raise StubRepositoryError until the matching
    # tables and orchestrators are added in the post-refactoring roadmap.
    # ------------------------------------------------------------------

    @property
    def job_repo(self) -> JobRepository:
        return self._job_repo

    @property
    def chunk_repo(self) -> ChunkRepository:
        return self._chunk_repo

    @property
    def prompt_repo(self) -> PromptRepository:
        return self._prompt_repo

    @property
    def conversation_repo(self) -> ConversationRepository:
        return self._conversation_repo

    @property
    def audit_log_repo(self) -> AuditLogRepository:
        return self._audit_log_repo

    @property
    def idempotency_repo(self) -> IdempotencyRepository:
        return self._idempotency_repo

    @property
    def entity_repo(self) -> EntityRepository:
        return self._entity_repo

    @property
    def topic_tag_repo(self) -> TopicTagRepository:
        return self._topic_tag_repo

    @property
    def model_endpoint_repo(self) -> ModelEndpointRepository:
        return self._model_endpoint_repo

    @property
    def preset_repo(self) -> PresetRepository:
        return self._preset_repo

    @property
    def evaluation_repo(self) -> EvaluationRepository:
        return self._evaluation_repo

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pool_getter(self) -> asyncpg.Pool:
        # Resolved at call time so repos can keep working after a
        # shutdown()/initialize() cycle in tests.
        return self._conn.pool


__all__ = ["PostgresStore"]
