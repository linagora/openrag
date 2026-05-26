"""Service container — wires registries and exposes component factories.

The container is the composition root for the refactored stack. It does
three things:

1. Populates the inference registries (Phase 6) so factory helpers can spin
   up embedders, LLMs, rerankers and VLMs by name.
2. Builds the storage adapters (Phase 7E) when a :class:`Settings` instance
   is supplied — a :class:`~core.ports.catalog_store.CatalogStore` and a
   :class:`~core.vector_stores.VectorStore`.
3. Owns the async :meth:`initialize` / :meth:`shutdown` lifecycle that opens
   and closes the asyncpg pool.

The ``settings`` argument is optional so the legacy test paths that only
care about registry side effects (``ServiceContainer()`` with no config)
keep working. Code that wants storage adapters must pass a
:class:`Settings` and ``await container.initialize()`` before issuing
queries.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.embeddings import embedder_registry
from core.llm import llm_registry
from core.rerankers import reranker_registry
from core.vlm import vlm_registry
from di.embedders import register_embedders
from di.llms import register_llms
from di.repositories import create_catalog_store
from di.rerankers import register_rerankers
from di.vector_stores import create_vector_store
from di.vlms import register_vlms

if TYPE_CHECKING:
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
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.partition_repo import PartitionRepository
    from core.ports.preset_repo import PresetRepository
    from core.ports.prompt_repo import PromptRepository
    from core.ports.topic_tag_repo import TopicTagRepository
    from core.ports.user_repo import UserRepository
    from core.ports.workspace_repo import WorkspaceRepository
    from core.vector_stores import VectorStore
    from services.orchestrators.auth_service import AuthService
    from services.orchestrators.conversion_service import ConversionService
    from services.orchestrators.indexing_service import IndexingService
    from services.orchestrators.job_service import JobService
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.query_service import QueryService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.user_service import UserService
    from services.orchestrators.workspace_service import WorkspaceService


_NO_SETTINGS_MESSAGE = (
    "ServiceContainer was constructed without a Settings instance — "
    "pass Settings to ServiceContainer(...) to wire storage adapters."
)


def _oidc_config_from_env():
    """Build :class:`OIDCConfig` from the same env vars ``main.py`` validates.

    Phase 8A.1 keeps OIDC config env-sourced (it is not yet wired into the
    root :class:`Settings`); ``enabled`` mirrors ``AUTH_MODE=oidc``.
    """
    from core.config.auth import OIDCConfig

    return OIDCConfig(
        enabled=os.getenv("AUTH_MODE", "token").strip().lower() == "oidc",
        issuer_url=os.getenv("OIDC_ENDPOINT", "") or "",
        client_id=os.getenv("OIDC_CLIENT_ID", "") or "",
        client_secret=os.getenv("OIDC_CLIENT_SECRET", "") or "",
        redirect_uri=os.getenv("OIDC_REDIRECT_URI", "") or "",
        scopes=os.getenv("OIDC_SCOPES", "openid email profile offline_access"),
        token_encryption_key=os.getenv("OIDC_TOKEN_ENCRYPTION_KEY", "") or "",
        claim_source=os.getenv("OIDC_CLAIM_SOURCE", "id_token").strip().lower(),
        claim_mapping=os.getenv("OIDC_CLAIM_MAPPING", "").strip(),
        post_logout_redirect_uri=os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI", "") or "",
        auto_provision_login=os.getenv("OIDC_AUTO_PROVISION_LOGIN", "false").strip().lower() == "true",
    )


class ServiceContainer:
    """Populates registries and provides typed factory access."""

    def __init__(self, settings: Settings | None = None) -> None:
        register_embedders()
        register_llms()
        register_rerankers()
        register_vlms()

        self._settings = settings
        self._catalog_store: CatalogStore | None = create_catalog_store(settings) if settings is not None else None
        self._vector_store: VectorStore | None = create_vector_store(settings) if settings is not None else None
        self._auth_service: AuthService | None = None
        self._user_service: UserService | None = None
        self._partition_service: PartitionService | None = None
        self._workspace_service: WorkspaceService | None = None
        self._retrieval_service: RetrievalService | None = None
        self._query_service: QueryService | None = None
        self._indexing_service: IndexingService | None = None
        self._job_service: JobService | None = None
        self._conversion_service: ConversionService | None = None

    def _require_settings(self) -> Settings:
        """Settings guard for the settings-dependent service properties.

        Without this, ``ServiceContainer()`` (no-settings legacy path)
        fails with a bare ``AttributeError`` on ``self._settings.x`` —
        inconsistent with the ``catalog_store`` / ``vector_store``
        contract, which raises ``RuntimeError(_NO_SETTINGS_MESSAGE)``.
        """
        if self._settings is None:
            raise RuntimeError(_NO_SETTINGS_MESSAGE)
        return self._settings

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the storage adapters (asyncpg pool + Alembic migrations)."""
        if self._catalog_store is not None:
            await self._catalog_store.initialize()
            await self.user_repo.ensure_admin_user(os.getenv("AUTH_TOKEN"))

    async def shutdown(self) -> None:
        """Close the storage adapters cleanly."""
        if self._catalog_store is not None:
            await self._catalog_store.shutdown()

    # ------------------------------------------------------------------
    # Storage adapters
    # ------------------------------------------------------------------

    @property
    def catalog_store(self) -> CatalogStore:
        if self._catalog_store is None:
            raise RuntimeError(_NO_SETTINGS_MESSAGE)
        return self._catalog_store

    @property
    def vector_store(self) -> VectorStore:
        """The Phase 7B :class:`MilvusVectorStore` built from settings.

        Cached at construction so repeated property reads return the same
        instance — every fresh build would open a new pymilvus gRPC
        channel.
        """
        if self._vector_store is None:
            raise RuntimeError(_NO_SETTINGS_MESSAGE)
        return self._vector_store

    # ------------------------------------------------------------------
    # Per-repo accessors (Phase 8 orchestrators take one repo, not the
    # whole store). All fifteen repos are exposed for symmetry and
    # grep-findability: shortcuts for the five real repos plus the ten
    # post-refactoring stubs.
    # ------------------------------------------------------------------

    @property
    def document_repo(self) -> DocumentRepository:
        return self.catalog_store.document_repo

    @property
    def user_repo(self) -> UserRepository:
        return self.catalog_store.user_repo

    @property
    def partition_repo(self) -> PartitionRepository:
        return self.catalog_store.partition_repo

    @property
    def membership_repo(self) -> PartitionMembershipRepository:
        return self.catalog_store.membership_repo

    @property
    def oidc_session_repo(self) -> OIDCSessionRepository:
        return self.catalog_store.oidc_session_repo

    @property
    def workspace_repo(self) -> WorkspaceRepository:
        return self.catalog_store.workspace_repo

    @property
    def job_repo(self) -> JobRepository:
        return self.catalog_store.job_repo

    @property
    def chunk_repo(self) -> ChunkRepository:
        return self.catalog_store.chunk_repo

    @property
    def prompt_repo(self) -> PromptRepository:
        return self.catalog_store.prompt_repo

    @property
    def conversation_repo(self) -> ConversationRepository:
        return self.catalog_store.conversation_repo

    @property
    def audit_log_repo(self) -> AuditLogRepository:
        return self.catalog_store.audit_log_repo

    @property
    def idempotency_repo(self) -> IdempotencyRepository:
        return self.catalog_store.idempotency_repo

    @property
    def entity_repo(self) -> EntityRepository:
        return self.catalog_store.entity_repo

    @property
    def topic_tag_repo(self) -> TopicTagRepository:
        return self.catalog_store.topic_tag_repo

    @property
    def model_endpoint_repo(self) -> ModelEndpointRepository:
        return self.catalog_store.model_endpoint_repo

    @property
    def preset_repo(self) -> PresetRepository:
        return self.catalog_store.preset_repo

    # ------------------------------------------------------------------
    # Orchestrators (Phase 8)
    # ------------------------------------------------------------------

    @property
    def auth_service(self) -> AuthService:
        """AuthService — lazily built, cached for the container's lifetime.

        The OIDC client is only constructed in ``AUTH_MODE=oidc`` (it reads
        required env vars and would raise otherwise); in token mode it is
        ``None`` and the OIDC flow methods refuse cleanly.
        """
        if self._auth_service is None:
            from services.orchestrators.auth_service import AuthService

            cfg = _oidc_config_from_env()
            client = None
            if cfg.enabled:
                from components.auth import get_oidc_client

                client = get_oidc_client()
            self._auth_service = AuthService(
                user_repo=self.user_repo,
                oidc_session_repo=self.oidc_session_repo,
                membership_repo=self.membership_repo,
                oidc_client=client,
                config=cfg,
            )
        return self._auth_service

    @property
    def user_service(self) -> UserService:
        """UserService — lazily built, cached for the container's lifetime."""
        if self._user_service is None:
            from services.orchestrators.user_service import UserService

            settings = self._require_settings()
            self._user_service = UserService(
                user_repo=self.user_repo,
                auth_service=self.auth_service,
                default_file_quota=settings.rdb.default_file_quota,
                partition_service=self.partition_service,
                membership_repo=self.membership_repo,
                job_service=self.job_service,
            )
        return self._user_service

    @property
    def partition_service(self) -> PartitionService:
        """PartitionService — lazily built, cached for the container's lifetime."""
        if self._partition_service is None:
            from services.orchestrators.partition_service import PartitionService

            settings = self._require_settings()
            self._partition_service = PartitionService(
                partition_repo=self.partition_repo,
                membership_repo=self.membership_repo,
                document_repo=self.document_repo,
                vector_store=self.vector_store,
                user_repo=self.user_repo,
                collection=settings.vectordb.collection_name,
            )
        return self._partition_service

    @property
    def workspace_service(self) -> WorkspaceService:
        """WorkspaceService — lazily built, cached for the container's lifetime."""
        if self._workspace_service is None:
            from services.orchestrators.workspace_service import WorkspaceService

            settings = self._require_settings()
            self._workspace_service = WorkspaceService(
                workspace_repo=self.workspace_repo,
                document_repo=self.document_repo,
                vector_store=self.vector_store,
                collection=settings.vectordb.collection_name,
            )
        return self._workspace_service

    @property
    def retrieval_service(self) -> RetrievalService:
        """RetrievalService — lazily built, cached for the container's lifetime."""
        if self._retrieval_service is None:
            from services.orchestrators.retrieval_service import RetrievalService
            from services.storage.vector_store_searcher import VectorStoreSearcher

            settings = self._require_settings()
            embed_cfg = settings.embedder
            embedder = self.create_embedder(
                "vllm",
                endpoint=embed_cfg.base_url,
                model_name=embed_cfg.model_name,
                api_key=embed_cfg.api_key,
                max_model_len=embed_cfg.max_model_len,
            )
            searcher = VectorStoreSearcher(
                vector_store=self.vector_store,
                embedder=embedder,
                document_repo=self.document_repo,
                collection=settings.vectordb.collection_name,
            )
            llm_cfg = settings.llm.model_dump()
            llm = self.create_llm(
                "vllm",
                endpoint=llm_cfg["base_url"],
                model_name=llm_cfg["model"],
                api_key=llm_cfg.get("api_key", ""),
                **{k: v for k, v in llm_cfg.items() if k not in ("base_url", "model", "api_key")},
            )
            reranker = None
            rcfg = settings.reranker
            if rcfg.enabled:
                reranker = self.create_reranker(
                    rcfg.provider,
                    endpoint=rcfg.base_url,
                    model_name=rcfg.model_name,
                    api_key=rcfg.api_key,
                    timeout=rcfg.timeout,
                )
            self._retrieval_service = RetrievalService(
                searcher=searcher,
                reranker=reranker,
                llm=llm,
                config=settings,
            )
        return self._retrieval_service

    @property
    def query_service(self) -> QueryService:
        """QueryService — lazily built, cached for the container's lifetime.

        Shares the same core LLM construction as ``retrieval_service``
        (built from ``settings.llm``); the web-search service comes from
        the legacy ``WebSearchFactory`` (provider is ``None`` when
        ``WEBSEARCH_API_TOKEN`` is unset — web search silently disabled).
        """
        if self._query_service is None:
            from components.websearch import WebSearchFactory
            from services.orchestrators.query_service import QueryService

            settings = self._require_settings()
            llm_cfg = settings.llm.model_dump()
            llm = self.create_llm(
                "vllm",
                endpoint=llm_cfg["base_url"],
                model_name=llm_cfg["model"],
                api_key=llm_cfg.get("api_key", ""),
                **{k: v for k, v in llm_cfg.items() if k not in ("base_url", "model", "api_key")},
            )
            self._query_service = QueryService(
                retrieval_service=self.retrieval_service,
                llm=llm,
                config=settings,
                web_search_service=WebSearchFactory.create_service(settings),
                workspace_service=self.workspace_service,
            )
        return self._query_service

    @property
    def indexing_service(self) -> IndexingService:
        """IndexingService — lazily built, cached for the container's lifetime.

        Phase 9B routes new indexing jobs through the thin ``IndexerPool``
        actor while delete/update/copy remain on the legacy actor path.
        """
        if self._indexing_service is None:
            from services.orchestrators.indexing_service import IndexingService
            from services.workers.dispatcher import from_ray_namespace

            settings = self._require_settings()
            self._indexing_service = IndexingService(
                document_repo=self.document_repo,
                workspace_repo=self.workspace_repo,
                dispatcher=from_ray_namespace(
                    vector_store=self.vector_store,
                    document_repo=self.document_repo,
                    workspace_repo=self.workspace_repo,
                    collection=settings.vectordb.collection_name,
                ),
            )
        return self._indexing_service

    @property
    def job_service(self) -> JobService:
        """JobService — lazily built, cached for the container's lifetime.

        Wraps the ``TaskStateManager`` Ray actor directly (8H excepts
        JobService); resolved lazily so the actor only needs to exist at
        first request.
        """
        if self._job_service is None:
            from services.orchestrators.job_service import JobService
            from services.workers.bootstrap import get_task_state_manager

            self._job_service = JobService(task_state_manager=get_task_state_manager())
        return self._job_service

    @property
    def conversion_service(self) -> ConversionService:
        """ConversionService — lazily built, cached for the container's lifetime.

        The serializer is the Ray-backed ``SerializerRayShim`` during the
        Phase-8 shim period (Ray cleanup is Phase 9); the DocSerializer
        actor is resolved lazily per call inside the shim.
        """
        if self._conversion_service is None:
            from services.orchestrators.conversion_service import ConversionService
            from services.workers.parsers.doc_serializer_adapter import from_ray_namespace

            settings = self._require_settings()
            self._conversion_service = ConversionService(
                serializer=from_ray_namespace(),
                vector_store=self.vector_store,
                collection=settings.vectordb.collection_name,
            )
        return self._conversion_service

    # ------------------------------------------------------------------
    # Registry-based inference factories (Phase 6)
    # ------------------------------------------------------------------

    @staticmethod
    def create_embedder(name: str = "vllm", **kwargs):
        return embedder_registry.create(name, **kwargs)

    @staticmethod
    def create_llm(name: str = "vllm", **kwargs):
        return llm_registry.create(name, **kwargs)

    @staticmethod
    def create_reranker(name: str = "infinity", **kwargs):
        return reranker_registry.create(name, **kwargs)

    @staticmethod
    def create_vlm(name: str = "vllm", **kwargs):
        return vlm_registry.create(name, **kwargs)
