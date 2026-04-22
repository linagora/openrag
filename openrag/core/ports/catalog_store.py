"""CatalogStore — aggregate root composing all repository ports.

Concrete implementations (e.g. PostgresStore) own the connection pool
and compose per-entity repository instances.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .audit_log_repo import AuditLogRepository
from .chunk_repo import ChunkRepository
from .conversation_repo import ConversationRepository
from .document_repo import DocumentRepository
from .entity_repo import EntityRepository
from .idempotency_repo import IdempotencyRepository
from .job_repo import JobRepository
from .model_endpoint_repo import ModelEndpointRepository
from .oidc_session_repo import OIDCSessionRepository
from .partition_repo import PartitionRepository
from .preset_repo import PresetRepository
from .prompt_repo import PromptRepository
from .topic_tag_repo import TopicTagRepository
from .user_repo import UserRepository


class CatalogStore(ABC):
    """Abstract interface for the relational catalog backing store.

    Concrete implementations (e.g. PostgresStore) live in the services layer.
    """

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @property
    @abstractmethod
    def document_repo(self) -> DocumentRepository: ...

    @property
    @abstractmethod
    def job_repo(self) -> JobRepository: ...

    @property
    @abstractmethod
    def user_repo(self) -> UserRepository: ...

    @property
    @abstractmethod
    def prompt_repo(self) -> PromptRepository: ...

    @property
    @abstractmethod
    def partition_repo(self) -> PartitionRepository: ...

    @property
    @abstractmethod
    def model_endpoint_repo(self) -> ModelEndpointRepository: ...

    @property
    @abstractmethod
    def preset_repo(self) -> PresetRepository: ...

    @property
    @abstractmethod
    def chunk_repo(self) -> ChunkRepository: ...

    @property
    @abstractmethod
    def entity_repo(self) -> EntityRepository: ...

    @property
    @abstractmethod
    def topic_tag_repo(self) -> TopicTagRepository: ...

    @property
    @abstractmethod
    def conversation_repo(self) -> ConversationRepository: ...

    @property
    @abstractmethod
    def audit_log_repo(self) -> AuditLogRepository: ...

    @property
    @abstractmethod
    def idempotency_repo(self) -> IdempotencyRepository: ...

    @property
    @abstractmethod
    def oidc_session_repo(self) -> OIDCSessionRepository: ...
