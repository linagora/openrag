"""Postgres persistence adapter — connection manager, schema, repositories.

This package contains the asyncpg-based Postgres adapter that replaces the
synchronous SQLAlchemy ORM in ``components/indexer/vectordb/utils.py``.

Public entry points:
    - :class:`connection.ConnectionManager` — pool lifecycle + migrations (7A.1)
    - :mod:`schema` — metadata-only Alembic target (7A.1)
    - Repositories (7A.2):
        - Real (decomposed from ``PartitionFileManager``):
            ``PgDocumentRepository``, ``PgUserRepository``,
            ``PgPartitionRepository``, ``PgPartitionMembershipRepository``,
            ``PgOIDCSessionRepository``, ``PgWorkspaceRepository``.
        - Stubs (post-refactoring features — raise
          :class:`StubRepositoryError` on every call):
            ``PgJobRepository``, ``PgChunkRepository``,
            ``PgPromptRepository``, ``PgConversationRepository``,
            ``PgAuditLogRepository``, ``PgIdempotencyRepository``,
            ``PgEntityRepository``, ``PgTopicTagRepository``,
            ``PgModelEndpointRepository``, ``PgPresetRepository``.
"""

from services.persistence._stubs import StubRepositoryError
from services.persistence.audit_log_repo import PgAuditLogRepository
from services.persistence.chunk_repo import PgChunkRepository
from services.persistence.connection import ConnectionManager
from services.persistence.conversation_repo import PgConversationRepository
from services.persistence.document_repo import PgDocumentRepository
from services.persistence.entity_repo import PgEntityRepository
from services.persistence.idempotency_repo import PgIdempotencyRepository
from services.persistence.job_repo import PgJobRepository
from services.persistence.model_endpoint_repo import PgModelEndpointRepository
from services.persistence.oidc_session_repo import PgOIDCSessionRepository
from services.persistence.partition_membership_repo import PgPartitionMembershipRepository
from services.persistence.partition_repo import PgPartitionRepository
from services.persistence.preset_repo import PgPresetRepository
from services.persistence.prompt_repo import PgPromptRepository
from services.persistence.schema import metadata
from services.persistence.topic_tag_repo import PgTopicTagRepository
from services.persistence.user_repo import PgUserRepository
from services.persistence.workspace_repo import PgWorkspaceRepository

__all__ = [
    "ConnectionManager",
    "PgAuditLogRepository",
    "PgChunkRepository",
    "PgConversationRepository",
    "PgDocumentRepository",
    "PgEntityRepository",
    "PgIdempotencyRepository",
    "PgJobRepository",
    "PgModelEndpointRepository",
    "PgOIDCSessionRepository",
    "PgPartitionMembershipRepository",
    "PgPartitionRepository",
    "PgPresetRepository",
    "PgPromptRepository",
    "PgTopicTagRepository",
    "PgUserRepository",
    "PgWorkspaceRepository",
    "StubRepositoryError",
    "metadata",
]
