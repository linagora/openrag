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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.embeddings import embedder_registry
from core.llm import llm_registry
from core.rerankers import reranker_registry
from core.utils.logging import get_logger
from core.vlm import vlm_registry
from di.embedders import register_embedders
from di.factories import make_component_factory
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
    from services.orchestrators.mcp_service import MCPService
    from services.orchestrators.model_endpoint_service import ModelEndpointService
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.preset_service import PresetService
    from services.orchestrators.query_service import QueryService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.user_service import UserService
    from services.orchestrators.workspace_service import WorkspaceService


logger = get_logger()

_NO_SETTINGS_MESSAGE = (
    "ServiceContainer was constructed without a Settings instance — "
    "pass Settings to ServiceContainer(...) to wire storage adapters."
)


class ServiceContainer:
    """Populates registries and provides typed factory access."""

    def __init__(self, settings: Settings | None = None) -> None:
        register_embedders()
        register_llms()
        register_rerankers()
        register_vlms()

        # Validate + freeze the OIDC env config now so misconfiguration
        # (AUTH_MODE=oidc with missing OIDC_*, invalid claim_source,
        # malformed claim_mapping) fails at construction rather than
        # silently at the first login. See OIDCConfig.from_env for the
        # full rule set.
        from core.config.auth import OIDCConfig

        self._oidc_config = OIDCConfig.from_env()

        self._settings = settings
        self._initialized = False
        self._inference_clients: list[Any] = []
        self._client_caches: list[dict[str, Any]] = []
        self._embedder_cache: dict[str, Any] = {}
        self._reranker_cache: dict[str, Any] = {}
        self._llm_cache: dict[str, Any] = {}
        self._vlm_cache: dict[str, Any] = {}
        self.embedder_factory = self._missing_named_factory("embedder")
        self.reranker_factory = self._missing_named_factory("reranker")
        self.llm_factory = self._missing_named_factory("llm")
        self.vlm_factory = self._missing_named_factory("vlm")
        self._catalog_store: CatalogStore | None = create_catalog_store(settings) if settings is not None else None
        self._vector_store: VectorStore | None = create_vector_store(settings) if settings is not None else None
        self._auth_service: AuthService | None = None
        self._user_service: UserService | None = None
        self._partition_service: PartitionService | None = None
        self._model_endpoint_service: ModelEndpointService | None = None
        self._preset_service: PresetService | None = None
        self._workspace_service: WorkspaceService | None = None
        self._retrieval_service: RetrievalService | None = None
        self._query_service: QueryService | None = None
        self._indexing_service: IndexingService | None = None
        self._job_service: JobService | None = None
        self._conversion_service: ConversionService | None = None
        self._mcp_service: MCPService | None = None
        if settings is not None:
            self._wire_named_component_factories(settings)

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

    def _missing_named_factory(self, _kind: str):
        def factory(_name: str = "default"):
            raise RuntimeError(_NO_SETTINGS_MESSAGE)

        return factory

    def _wire_named_component_factories(self, settings: Settings) -> None:
        """Wire Phase 14 named inference factories from ``settings.models``."""
        models = settings.models
        embed_defaults = settings.embedder

        def _embedder_extra_kwargs(cfg: Any) -> dict[str, Any]:
            """Backfill max_model_len/embed_concurrency from static settings when the
            endpoint's ``extra`` omits them (an explicit ``extra`` value wins)."""
            defaults: dict[str, Any] = {}
            if "max_model_len" not in cfg.extra:
                defaults["max_model_len"] = embed_defaults.max_model_len
            if "embed_concurrency" not in cfg.extra:
                defaults["embed_concurrency"] = embed_defaults.embed_concurrency
            return defaults

        self.embedder_factory, self._embedder_cache = make_component_factory(
            registry=embedder_registry,
            config_section=models.embedder,
            default_impl="vllm",
            client_caches=self._client_caches,
            extra_kwargs_fn=_embedder_extra_kwargs,
        )
        self.reranker_factory, self._reranker_cache = make_component_factory(
            registry=reranker_registry,
            config_section=models.reranker,
            default_impl="infinity",
            client_caches=self._client_caches,
        )
        self.llm_factory, self._llm_cache = make_component_factory(
            registry=llm_registry,
            config_section=models.llm,
            default_impl="vllm",
            client_caches=self._client_caches,
        )
        self.vlm_factory, self._vlm_cache = make_component_factory(
            registry=vlm_registry,
            config_section=models.vlm,
            default_impl="vllm",
            client_caches=self._client_caches,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the storage adapters (asyncpg pool + Alembic migrations)."""
        if self._catalog_store is not None:
            await self._initialize_step("initializing catalog store", self._catalog_store.initialize)
            await self._initialize_step(
                "ensuring admin user",
                lambda: self.user_repo.ensure_admin_user(os.getenv("AUTH_TOKEN")),
            )
            await self._initialize_step("seeding model endpoints", self.model_endpoint_service.seed_defaults)
            await self._initialize_step("loading model endpoints", self.model_endpoint_service.load_all)
            await self._initialize_step("seeding pipeline presets", self.preset_service.seed_defaults)
            await self._initialize_step("loading pipeline presets", self.preset_service.load_all)
            await self._initialize_step("ensuring default partition", self.partition_service.seed_default_partition)
            await self._initialize_step("loading partition configs", self.partition_service.load_partitions)
        self._initialized = True

    async def _initialize_step(self, label: str, operation: Callable[[], Awaitable[Any]]) -> None:
        """Run one startup step with consistent failure logging."""
        logger.info(f"ServiceContainer.initialize: {label}")
        try:
            await operation()
        except Exception:
            logger.exception("ServiceContainer initialization step failed", step=label)
            raise

    async def shutdown(self) -> None:
        """Close inference clients and storage adapters cleanly.

        Best-effort: a failure closing one client must not skip the
        remaining clients, the database pool, or the state reset.
        """
        try:
            seen_client_ids: set[int] = set()
            for client in self._inference_clients:
                await self._close_inference_client(client, seen_client_ids)
            for cache in self._client_caches:
                for client in list(cache.values()):
                    await self._close_inference_client(client, seen_client_ids)
                cache.clear()
            if self._catalog_store is not None:
                await self._catalog_store.shutdown()
        finally:
            self._inference_clients.clear()
            self._initialized = False

    async def _close_inference_client(self, client: Any, seen_client_ids: set[int]) -> None:
        """Close one tracked inference client once, best-effort."""
        client_id = id(client)
        if client_id in seen_client_ids:
            return
        seen_client_ids.add(client_id)
        aclose = getattr(client, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            logger.exception("Failed to close inference client")

    @property
    def is_initialized(self) -> bool:
        """True once :meth:`initialize` has completed its async I/O."""
        return self._initialized

    @property
    def config(self) -> Settings:
        """The root settings this container was wired from."""
        return self._require_settings()

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

            cfg = self._oidc_config
            client = None
            if cfg.enabled:
                from services.auth import get_oidc_client

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
            from services.workers.bootstrap import get_task_state_manager

            settings = self._require_settings()
            self._partition_service = PartitionService(
                partition_repo=self.partition_repo,
                membership_repo=self.membership_repo,
                document_repo=self.document_repo,
                vector_store=self.vector_store,
                user_repo=self.user_repo,
                collection=settings.vectordb.collection_name,
                config=settings,
                task_state_manager_factory=get_task_state_manager,
            )
        return self._partition_service

    @property
    def model_endpoint_service(self) -> ModelEndpointService:
        """ModelEndpointService — DB-backed named model endpoint registry."""
        if self._model_endpoint_service is None:
            from services.orchestrators.model_endpoint_service import ModelEndpointService

            self._model_endpoint_service = ModelEndpointService(
                model_endpoint_repo=self.model_endpoint_repo,
                config=self._require_settings(),
                partition_service=self.partition_service,
                client_caches={
                    "embedder": self._embedder_cache,
                    "reranker": self._reranker_cache,
                    "llm": self._llm_cache,
                    "vlm": self._vlm_cache,
                },
            )
        return self._model_endpoint_service

    @property
    def preset_service(self) -> PresetService:
        """PresetService — DB-backed pipeline preset registry."""
        if self._preset_service is None:
            from services.orchestrators.preset_service import PresetService

            self._preset_service = PresetService(
                preset_repo=self.preset_repo,
                config=self._require_settings(),
                partition_service=self.partition_service,
            )
        return self._preset_service

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
                timeout=embed_cfg.timeout,
                batch_size=embed_cfg.batch_size,
                embed_concurrency=embed_cfg.embed_concurrency,
            )
            searcher = VectorStoreSearcher(
                vector_store=self.vector_store,
                embedder=embedder,
                document_repo=self.document_repo,
                collection=settings.vectordb.collection_name,
            )

            def searcher_factory(embedder_name: str):
                return VectorStoreSearcher(
                    vector_store=self.vector_store,
                    embedder=self.embedder_factory(embedder_name),
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
                searcher_factory=searcher_factory,
                reranker_factory=self.reranker_factory,
                llm_factory=self.llm_factory,
            )
        return self._retrieval_service

    @property
    def query_service(self) -> QueryService:
        """QueryService — lazily built, cached for the container's lifetime.

        Shares the same core LLM construction as ``retrieval_service``
        (built from ``settings.llm``); the named ``llm_factory`` lets the
        service honor a partition's ``chat_llm`` model-endpoint preset for
        query generation and answer generation. The web-search service
        comes from the
        ``WebSearchFactory`` (provider is ``None`` when
        ``WEBSEARCH_API_TOKEN`` is unset — web search silently disabled).
        """
        if self._query_service is None:
            from services.orchestrators.query_service import QueryService
            from services.websearch import WebSearchFactory

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
                llm_factory=self.llm_factory,
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
                    job_repo=self.job_repo,
                ),
                config=settings,
                partition_service=self.partition_service,
            )
        return self._indexing_service

    @property
    def job_service(self) -> JobService:
        """JobService — lazily built, cached for the container's lifetime.

        Reads the durable ``jobs`` table, with the ``TaskStateManager`` Ray
        actor as the fallback (8H excepts JobService for wrapping the actor);
        resolved lazily so the actor only needs to exist at first request.
        """
        if self._job_service is None:
            from services.orchestrators.job_service import JobService
            from services.workers.bootstrap import get_task_state_manager

            self._job_service = JobService(
                task_state_manager=get_task_state_manager(),
                job_repo=self.job_repo,
            )
        return self._job_service

    @property
    def conversion_service(self) -> ConversionService:
        """ConversionService — lazily built, cached for the container's lifetime.

        The serializer is the in-process ``ParserFileSerializer`` — it runs the
        parser dispatcher directly (GPU backends still dispatch to their pool
        actors) and implements the ``FileSerializer`` port, so the orchestrator
        stays decoupled from the parser/Ray infrastructure.
        """
        if self._conversion_service is None:
            from services.orchestrators.conversion_service import ConversionService
            from services.workers.parsers.file_serializer import build_file_serializer

            settings = self._require_settings()
            self._conversion_service = ConversionService(
                serializer=build_file_serializer(),
                vector_store=self.vector_store,
                collection=settings.vectordb.collection_name,
            )
        return self._conversion_service

    @property
    def mcp_service(self) -> MCPService:
        """MCPService — lazily built, cached for the container's lifetime.

        Composes the retrieval/partition/indexing/job/conversion
        orchestrators plus the vector-store port into the application layer
        the standalone MCP server (``api/mcp``) drives. Search defaults and
        bounds come from the ``mcp`` settings section.
        """
        if self._mcp_service is None:
            from services.orchestrators.mcp_service import MCPService

            settings = self._require_settings()
            mcp_cfg = settings.mcp
            self._mcp_service = MCPService(
                retrieval_service=self.retrieval_service,
                partition_service=self.partition_service,
                indexing_service=self.indexing_service,
                job_service=self.job_service,
                conversion_service=self.conversion_service,
                auth_service=self.auth_service,
                vector_store=self.vector_store,
                collection=settings.vectordb.collection_name,
                default_file_quota=settings.rdb.default_file_quota,
                default_top_k=mcp_cfg.default_top_k,
                max_top_k=mcp_cfg.max_top_k,
                similarity_threshold=mcp_cfg.similarity_threshold,
                download_timeout=mcp_cfg.download_timeout,
                max_download_bytes=mcp_cfg.max_download_bytes,
            )
        return self._mcp_service

    # ------------------------------------------------------------------
    # Registry-based inference factories (Phase 6)
    # ------------------------------------------------------------------

    def create_embedder(self, name: str = "vllm", **kwargs):
        """Build an embedder client, tracking it for shutdown cleanup."""
        return self._track(embedder_registry.create(name, **kwargs))

    def create_llm(self, name: str = "vllm", **kwargs):
        """Build an LLM client, tracking it for shutdown cleanup."""
        return self._track(llm_registry.create(name, **kwargs))

    def create_reranker(self, name: str = "infinity", **kwargs):
        """Build a reranker client, tracking it for shutdown cleanup."""
        return self._track(reranker_registry.create(name, **kwargs))

    def create_vlm(self, name: str = "vllm", **kwargs):
        """Build a VLM client, tracking it for shutdown cleanup."""
        return self._track(vlm_registry.create(name, **kwargs))

    def _track(self, client: Any) -> Any:
        """Register a built inference client so :meth:`shutdown` can close it."""
        self._inference_clients.append(client)
        return client
