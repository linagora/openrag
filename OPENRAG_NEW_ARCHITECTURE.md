# OPENRAG_NEW_ARCHITECTURE

# Structuring a Production-Grade RAG System

## A deep dive into new OpenRAG architecture, design patterns, and data flows

---

## Table of Contents

1. [The Problem](about:blank#1-the-problem)
2. [High-Level Architecture](about:blank#2-high-level-architecture)
3. [Project Structure](about:blank#3-project-structure)
    - [Interfaces vs Ports](about:blank#interfaces-vs-ports-two-kinds-of-abstraction-boundaries)
4. [The 3-Layer Rule](about:blank#4-the-3-layer-rule)
5. [Design Pattern 1: Abstract Interfaces (Dependency Inversion)](about:blank#5-design-pattern-1-abstract-interfaces)
6. [Design Pattern 2: Generic Registry](about:blank#6-design-pattern-2-generic-registry)
7. [Design Pattern 3: Cached Component Factory](about:blank#7-design-pattern-3-cached-component-factory)
8. [Design Pattern 4: Strategy Pattern (Retrieval Pipelines)](about:blank#8-design-pattern-4-strategy-pattern)
9. [Design Pattern 5: Repository Pattern (Ports & Adapters)](about:blank#9-design-pattern-5-repository-pattern)
10. [The DI Container: Three Files, Three Responsibilities](about:blank#10-the-di-container-three-files-three-responsibilities)
    - [10.1 container.py — The Composition Root](about:blank#101-containerpy--the-composition-root)
    - [10.2 factories.py — Config-Driven Lazy Instantiation](about:blank#102-factoriespy--config-driven-lazy-instantiation)
    - [10.3 providers.py — The FastAPI Bridge](about:blank#103-providerspy--the-fastapi-bridge)
    - [10.4 How the three files collaborate](about:blank#104-how-the-three-files-collaborate)
    - [10.5 Why this design avoids common DI anti-patterns](about:blank#105-why-this-design-avoids-common-di-anti-patterns)
11. [Configuration Architecture](about:blank#11-configuration-architecture)
12. [Flow 1: Document Indexing](about:blank#12-flow-1-document-indexing)
13. [Flow 2: Query Retrieval](about:blank#13-flow-2-query-retrieval)
14. [Flow 3: Chat Completion (OpenAI-compatible)](about:blank#14-flow-3-chat-completion)
15. [Flow 4: Adding a New Component](about:blank#15-flow-4-adding-a-new-component)
16. [Multi-Tenancy: Per-Partition Configuration](about:blank#16-multi-tenancy)
17. [Observability Architecture](about:blank#17-observability-architecture)
18. [Key Takeaways](about:blank#18-key-takeaways)

---

## 1. The Problem

Building a production RAG system is not just about chaining an embedder, a vector DB, and an LLM. In production you need:

- **Multiple embedding providers** (vLLM, Ollama, OpenAI) — swappable without code changes
- **Multiple retrieval strategies** (simple, multiquery, HyDE) — selectable per partition
- **Multiple vector databases** (Milvus by default, another Vector DB on demand) — replaceable
- **Fine-grained configuration** — each partition has its own pipeline configuration
- **Distributed processing** — Ray for GPU-heavy PDF parsing, parallel embedding
- **Observability** — Prometheus metrics on every operation
- **Testability** — the core logic must be testable without any infrastructure running

The question is: **how do you structure the code so that all of this is modular, decoupled, and maintainable?**

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        API Layer (FastAPI)                          │
│  Thin controllers — auth, validation, schema mapping                │
│  routers/admin/  routers/user/  routers/auth/  schemas/             │
├─────────────────────────────────────────────────────────────────────┤
│                       Services Layer                                │
│  Infrastructure adapters — HTTP clients, Postgres, Milvus, S3, Ray  │
│  orchestrators/  persistence/  storage/  inference/  workers/       │
├─────────────────────────────────────────────────────────────────────┤
│                        Core Layer                                   │
│  Pure domain logic — ZERO infrastructure imports                    │
│  interfaces/  models/  config/  retrieval/  chunking/  indexing/    │
└─────────────────────────────────────────────────────────────────────┘
         ▲                    ▲                    ▲
         │                    │                    │
    Dependency            Dependency           Dependency
    flows INWARD          flows INWARD         flows INWARD
```

**The dependency rule**: every arrow points inward. `core` knows nothing about `services`. `services` knows nothing about `api`. This is enforced — zero import violations.

---

## 3. Project Structure

```
openrag/
├── core/                              # Layer 1: Pure domain logic
│   ├── interfaces/                    #   ABCs: Embedder, LLM, VLM, Reranker, VectorStore
│   ├── ports/                         #   Repository ABCs: DocumentRepo, ChunkRepo, JobRepo
│   ├── models/                        #   Domain models: Document, Chunk, Query, User
│   ├── config/                        #   Typed Pydantic config + YAML loader
│   ├── chunking/                      #   Chunking strategies (markdown, recursive, sentence)
│   ├── retrieval/                     #   Retrieval pipelines (simple, multiquery, HyDE)
│   │   └── pipelines/                 #     Strategy pattern + orchestrator
│   ├── indexing/                      #   Document parsers (PDF, HTML, image, audio, video)
│   │   └── parsers/                   #     Parser registry + implementations
│   ├── llm/                           #   LLM registry + prompt builder
│   ├── embeddings/                    #   Embedder registry (empty — implementations in services)
│   ├── rerankers/                     #   Reranker registry
│   ├── vlm/                           #   VLM registry + captioning utils
│   ├── observability/                 #   Prometheus metric definitions
│   └── utils/                         #   Registry[T], exceptions, logging, tracing
│
├── services/                          # Layer 2: Infrastructure adapters
│   ├── orchestrators/                 #   Business workflows (indexing, retrieval, auth, jobs)
│   ├── persistence/                   #   Postgres repositories (asyncpg)
│   ├── storage/                       #   Milvus vector store, Postgres catalog, S3
│   ├── inference/                     #   HTTP clients: vLLM, Ollama, Infinity
│   ├── workers/                       #   Ray pipeline stages
│   │   └── stages/                    #     parse → chunk → caption → contextualize → embed → store
│   └── events/                        #   In-process SSE event bus
│
├── di/                                # Dependency injection wiring
│   ├── container.py                   #   ServiceContainer: singleton, lifecycle
│   ├── providers.py                   #   FastAPI Depends() accessors
│   ├── factories.py                   #   Generic cached component factory
│   └── embedders/llms/rerankers/vlms/pipelines/repositories/vector_stores.py
│
├── api/                               # Layer 3: HTTP boundary
│   ├── main.py                        #   FastAPI app, lifespan, middleware
│   ├── dependencies/auth.py           #   JWT + API key auth
│   ├── middleware/                     #   Instrumentation, security headers
│   ├── routers/{admin,auth,user}/     #   Thin route handlers
│   └── schemas/{admin,auth,user}/     #   Request/response Pydantic models
│
├── prompts/templates/                 #   Prompt templates (seeded to DB on boot)
├── conf/                              #   YAML config files per environment
├── infra/                             #   Docker, Compose, Grafana, Prometheus, nginx
├── tests/                             #   Unit + integration + load tests
└── ui/                                #   React admin dashboard
```

### Why this matters

Every file has one obvious home. A developer can answer “where does this go?” by asking:

- Does it import `httpx`, `asyncpg`, `pymilvus`, `ray`? → `services/`
- Is it a Pydantic model, ABC, or pure algorithm? → `core/`
- Does it handle HTTP requests/responses? → `api/`

### Interfaces vs Ports: two kinds of abstraction boundaries

The `core/` layer has two separate directories for ABCs — `interfaces/` and `ports/`. They look similar (both are abstract base classes), but they serve fundamentally different architectural roles.

**Interfaces** define contracts for **infrastructure capabilities** — external services the system *calls out to*:

```
core/interfaces/
├── embedder.py           # "I can turn text into vectors"
├── llm.py                # "I can generate text"
├── vlm.py                # "I can describe images"
├── reranker.py           # "I can re-score search results"
├── vector_store.py       # "I can store and search vectors"
├── chunking_strategy.py  # "I can split documents into chunks"
└── document_parser.py    # "I can parse a document format"
```

These abstract *technology choices*. Swapping vLLM for Ollama means writing a new `Embedder` implementation.

**Ports** define contracts for **data persistence** — the Repository pattern for storage:

```
core/ports/
├── document_repo.py        # CRUD for documents
├── chunk_repo.py           # Bulk operations for chunks
├── job_repo.py             # Job lifecycle tracking
├── user_repo.py            # Users + API keys + partition assignments
├── prompt_repo.py          # Prompt CRUD + active resolution
├── entity_repo.py          # Entity dictionary + mentions
├── topic_tag_repo.py       # Document topic tags
├── partition_repo.py       # Partition config persistence
├── preset_repo.py          # Pipeline preset storage
└── model_endpoint_repo.py  # Model endpoint config storage
```

These abstract *where data lives*. Every port follows the same CRUD pattern: `create_*`, `get_*`, `list_*`, `update_*`, `delete_*`.

**Why not put them in the same folder?** Because they have different extension points and different DI wiring:

| Aspect | Interfaces | Ports |
| --- | --- | --- |
| What they abstract | External AI/infra services | Data storage |
| Extension pattern | New provider = new file + `@registry.register()` | New DB = new adapter implementing the port |
| DI resolution | Registry → Factory (lazy, cached, config-driven) | Repository composition (shared connection pool) |
| Typical swap | vLLM → Ollama, Milvus → Qdrant | Postgres → MySQL, real DB → in-memory mock |
| How many impls | Multiple at runtime (per config) | One at runtime (but mockable in tests) |

The key insight: a single retrieval request might use 3 different **interface** implementations simultaneously (vLLM embedder + Infinity reranker + Milvus vector store), but always goes through the same **port** implementation (Postgres repositories). Interfaces are *horizontally diverse*; ports are *vertically consistent*.

---

## 4. The 3-Layer Rule

### Layer 1: Core (zero infrastructure imports)

```python
# core/interfaces/embedder.py — Pure ABC, no httpx, no vLLM, nothing

class Embedder(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    async def embed_single(self, text: str) -> list[float]: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...
```

This interface says: *“I can embed text.”* It doesn’t say *how*. The `core` layer defines **what** the system does. The `services` layer defines **how**.

### Layer 2: Services (implements core interfaces)

```python
# services/inference/vllm_client.py — Implements the interface with real HTTP

@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder):
    def __init__(self, endpoint: str, model_name: str, batch_size: int = 32, **kwargs):
        self._client = httpx.AsyncClient(...)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(f"{self._endpoint}/v1/embeddings", ...)
        return [item["embedding"] for item in resp.json()["data"]]
```

`httpx` lives here. The core layer never sees it.

### Layer 3: API (thin controllers)

```python
# api/routers/user/retrieve.py — Thin controller

@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: RetrieveRequest,
    current_user: User = Depends(get_current_user),
    service: RetrievalService = Depends(get_retrieval_service),
) -> RetrieveResponse:
    response = await service.retrieve(query=request.query, partition=request.partition)
    return RetrieveResponse(...)
```

The route reads the request, calls the service, maps the response. No business logic.

### Verification

You can verify the rule holds by running:

```bash
grep -r "from openrag.services" openrag/core/    # → zero results
grep -r "from openrag.api" openrag/services/     # → zero results
grep -r "from openrag.api" openrag/core/         # → zero results
```

---

## 5. Design Pattern 1: Abstract Interfaces

Every pluggable component has an ABC in `core/interfaces/`:

```
core/interfaces/
├── embedder.py      # Embedder ABC       — 3 abstract methods
├── llm.py           # LLM ABC            — 2 abstract + 2 default methods
├── vlm.py           # VLM ABC            — 2 abstract methods
├── reranker.py      # Reranker ABC       — 1 abstract method
├── vector_store.py  # VectorStore ABC    — 8 abstract methods
├── catalog_store.py # CatalogStore ABC   — composite repository interface
├── chunking_strategy.py                  — 1 abstract method
└── document_parser.py                    — 2 abstract methods
```

The `VectorStore` interface is what makes it possible to swap Milvus for Qdrant if needed:

```python
# core/interfaces/vector_store.py

class VectorStore(ABC):
    @abstractmethod
    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int: ...

    @abstractmethod
    async def search(
        self, embedding: list[float], top_k: int = 10,
        collection: str = "default", filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def delete(self, ids: list[str], collection: str = "default") -> int: ...

    @abstractmethod
    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None: ...
    # ... 4 more abstract methods
```

Every service that needs vector search depends on `VectorStore`, not on `MilvusVectorStore`. The swap happens in one file (`di/vector_stores.py`).

---

## 6. Design Pattern 2: Generic Registry

The Registry is the backbone of the plugin system. One generic class, used 7 times:

```python
# core/utils/registry.py

class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._registry: dict[str, Type[T]] = {}

    def register(self, name: str):
        """Decorator to register a class under a name."""
        def decorator(cls: Type[T]) -> Type[T]:
            self._registry[name] = cls
            return cls
        return decorator

    def create(self, name: str, **kwargs: Any) -> T:
        """Instantiate a registered class by name."""
        cls = self._registry.get(name)
        if cls is None:
            available = ", ".join(sorted(self._registry.keys()))
            raise RegistryError(f"{self._kind} '{name}' not found. Available: [{available}]")
        return cls(**kwargs)
```

### 7 typed registry instances

```python
# Each in core/ — empty catalog, no implementations yet
embedder_registry:  Registry[Embedder]            = Registry("embedder")
llm_registry:       Registry[LLM]                 = Registry("llm")
reranker_registry:  Registry[Reranker]            = Registry("reranker")
vlm_registry:       Registry[VLM]                 = Registry("vlm")
pipeline_registry:  Registry[RetrievalPipeline]   = Registry("retrieval_pipeline")
chunking_registry:  Registry[ChunkingStrategy]    = Registry("chunking_strategy")
parser_registry:    Registry[DocumentParser]      = Registry("document_parser")
```

### How implementations register

```python
# services/inference/vllm_client.py — self-registers on import

@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder): ...

@llm_registry.register("vllm")
class VLLMClient(LLM): ...
```

```python
# services/inference/ollama_client.py

@embedder_registry.register("ollama")
class OllamaEmbedder(Embedder): ...
```

```python
# core/retrieval/pipelines/simple.py

@pipeline_registry.register("simple")
class SimplePipeline(RetrievalPipeline): ...
```

### Triggering registration at boot

Registrations happen via Python’s import side-effect mechanism. The DI layer triggers them:

```python
# di/embedders.py
def register_embedders() -> None:
    import openrag.services.inference.vllm_client    # @register("vllm") runs
    import openrag.services.inference.ollama_client  # @register("ollama") runs
```

Called once during container construction:

```python
# di/container.py
class ServiceContainer:
    def __init__(self):
        register_embedders()   # populates embedder_registry
        register_rerankers()   # populates reranker_registry
        register_llms()        # populates llm_registry
        register_vlms()        # populates vlm_registry
        register_pipelines()   # populates pipeline_registry
```

---

## 7. Design Pattern 3: Cached Component Factory

The factory bridges **YAML config** and **registries** — given a model name like `"bge-m3"`, it looks up the endpoint config, resolves the implementation, and caches the instance:

```python
# di/factories.py

def make_component_factory(
    registry: Registry[T],
    config_section: dict[str, ModelEndpointConfig],
    default_impl: str,
    client_caches: list[dict[str, T]],
) -> Callable[[str], T]:

    cache: dict[str, T] = {}
    lock = threading.Lock()

    def factory(name: str) -> T:
        if name in cache:
            return cache[name]
        with lock:                                    # thread-safe singleton
            if name in cache:
                return cache[name]
            model_cfg = config_section[name]          # lookup YAML config
            impl = model_cfg.extra.get("implementation", default_impl)
            client = registry.create(impl,            # registry resolves class
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
            )
            cache[name] = client
            return client

    return factory
```

### One factory per component type

```python
# di/container.py

self.embedder_factory = make_component_factory(
    registry=embedder_registry,
    config_section=config.models.embedder,   # {"bge-m3": ModelEndpointConfig(...)}
    default_impl="vllm",
)

self.llm_factory = make_component_factory(
    registry=llm_registry,
    config_section=config.models.llm,        # {"mistral-small": ModelEndpointConfig(...)}
    default_impl="vllm",
)
```

### The resolution chain

```
factory("bge-m3")
  → config.models.embedder["bge-m3"]
    → endpoint="http://ray-head:8000", implementation="vllm"
  → embedder_registry.create("vllm", endpoint="http://ray-head:8000", model_name="bge-m3")
    → VLLMEmbedder(endpoint="http://ray-head:8000", model_name="bge-m3")
  → cached for next call
```

---

## 8. Design Pattern 4: Strategy Pattern (Retrieval Pipelines)

Three retrieval strategies, selected at runtime per partition:

```python
# core/retrieval/pipelines/base.py

class RetrievalPipeline(ABC):
    @abstractmethod
    async def execute(
        self,
        query: RetrievalQuery,
        embedder: Embedder,
        vector_store: VectorStore,
        reranker: Reranker | None = None,
        llm: LLM | None = None,
        trace: PipelineTrace | None = None,
        prompt_override: str | None = None,
        chunk_repo: ChunkRepository | None = None,
    ) -> list[ScoredChunk]: ...
```

All dependencies are **passed as arguments** — not from the registry, not from globals. This makes every pipeline independently testable.

### SimplePipeline

```
Query → Embed → Vector Search → Hydrate → Rerank → Return
```

### MultiQueryPipeline

```
Query → LLM generates N variations → Embed all → Search all (bounded concurrency)
      → Hydrate batch → Reciprocal Rank Fusion → Rerank → Return
```

### HyDEPipeline

```
Query → LLM generates hypothetical answer → Embed hypothetical + original
      → Search both (parallel) → Merge with HyDE weight boost → Rerank → Return
```

### Pipeline selection flow

```python
# core/retrieval/pipelines/orchestrator.py

class PipelineOrchestrator:
    async def execute(self, query, pipeline_config, embedder, vector_store, ...):
        pipeline_type = query.pipeline or pipeline_config.type  # "simple" / "multiquery" / "hyde"

        pipeline = pipeline_registry.create(pipeline_type, **pipeline_config.extra)

        scored_chunks = await pipeline.execute(
            query=query,
            embedder=embedder,
            vector_store=vector_store,
            reranker=reranker,
            llm=llm,
        )
        # Convert ScoredChunk → RetrievalResult, record metrics
        ...
```

The orchestrator doesn’t know which pipeline it’s running. It asks the registry.

---

## 9. Design Pattern 5: Repository Pattern (Ports & Adapters)

10 repository ABCs in `core/ports/`, implemented by Postgres adapters in `services/persistence/`:

```python
# core/ports/document_repo.py — Pure contract

class DocumentRepository(ABC):
    @abstractmethod
    async def create_document(self, doc: DocumentRecord) -> DocumentRecord: ...

    @abstractmethod
    async def get_document(self, document_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    async def list_documents(self, partition=None, status=None, offset=0, limit=50): ...

    @abstractmethod
    async def update_document(self, document_id: str, **fields) -> DocumentRecord | None: ...

    @abstractmethod
    async def delete_document(self, document_id: str) -> bool: ...
```

```python
# services/persistence/document_repo.py — Postgres implementation

class PgDocumentRepository(DocumentRepository):
    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    async def create_document(self, doc: DocumentRecord) -> DocumentRecord:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO documents (...) VALUES ($1, $2, ...)",
                doc.id, doc.filename, ...
            )
        return doc
```

### Composite CatalogStore

All 10 repositories are composed into a single `PostgresStore` facade:

```python
# services/storage/postgres_store.py

class PostgresStore(CatalogStore):
    def __init__(self, config):
        self._conn = ConnectionManager(config)
        pool_getter = lambda: self._conn.pool

        self._documents = PgDocumentRepository(pool_getter)
        self._jobs = PgJobRepository(pool_getter)
        self._chunks = PgChunkRepository(pool_getter)
        self._users = PgUserRepository(pool_getter)
        # ... 6 more repositories
```

All share the same connection pool. Each is independently testable via its ABC.

---

## 10. The DI Container: Three Files, Three Responsibilities

The `di/` directory has three core files. Each has a distinct role in the dependency injection system:

```
di/
├── container.py    # WHAT to wire — the composition root
├── factories.py    # HOW to create — lazy, cached, config-driven instantiation
└── providers.py    # WHERE to inject — FastAPI Depends() bridge
```

### 10.1 `container.py` — The Composition Root

**Purpose**: The single place where all dependencies are assembled. Constructed once at startup as a singleton. No service creates its own dependencies — they all receive them here.

**Concept**: This is the “composition root” pattern from DI theory. The container knows about *every* concrete class, but nothing else in the system does. Services only see ABCs.

The container has a **two-phase lifecycle**:

```python
# di/container.py

class ServiceContainer:
    """Central DI container. Instantiated once at startup."""

    def __init__(self, config: OpenRAGConfig | None = None) -> None:
        # ─── Phase 1: Synchronous construction ─────────────────────

        self._config = config or load_config()
        self._initialized = False

        # Step 1: Populate registries (import side-effects trigger @register decorators)
        register_embedders()     # imports vllm_client.py → @embedder_registry.register("vllm")
        register_rerankers()     # imports infinity_client.py → @reranker_registry.register("infinity")
        register_llms()          # imports vllm_client.py → @llm_registry.register("vllm")
        register_vlms()          # imports vlm_client.py → @vlm_registry.register("vllm-vision")
        register_pipelines()     # imports simple/multiquery/hyde → all registered

        # Step 2: Create infrastructure adapters (concrete implementations)
        self.vector_store = create_vector_store(self._config.milvus)   # → MilvusVectorStore
        self.s3_store = S3Store(self._config.s3)                       # → S3Store
        self.postgres_store = create_catalog_store(self._config.postgres)  # → PostgresStore

        # Step 3: Create component factories (registry + config → lazy singletons)
        self.embedder_factory = make_component_factory(
            registry=embedder_registry,
            config_section=self._config.models.embedder,
            default_impl="vllm",
            client_caches=self._client_caches,
        )
        # ... same for reranker_factory, llm_factory, vlm_factory

        # Step 4: Wire high-level services (injecting ABCs, not concrete classes)
        self.retrieval_service = RetrievalService(
            vector_store=self.vector_store,           # VectorStore ABC
            embedder_factory=self.embedder_factory,   # Callable[[str], Embedder]
            reranker_factory=self.reranker_factory,    # Callable[[str], Reranker]
            llm_factory=self.llm_factory,              # Callable[[str], LLM]
            config=self._config,
            chunk_repo=self.chunk_repo,                # ChunkRepository ABC
        )
        # ... same for indexing_service, document_service, job_service, auth_service, etc.

    # ─── Phase 2: Async initialization (called from FastAPI lifespan) ──

    async def initialize(self) -> None:
        await self.postgres_store.initialize()          # create connection pool
        self.postgres_store.run_migrations()             # run Alembic migrations
        await self.model_endpoint_service.seed_defaults() # seed default model endpoints
        await self.model_endpoint_service.load_all()     # populate config from DB
        await self.preset_service.seed_defaults()        # seed default presets
        await self.preset_service.load_all()             # populate config from DB
        await self.partition_service.seed_default_partition()
        await self.partition_service.load_partitions()   # resolve presets → full PartitionConfig
        await self.auth_service.seed_superadmin()        # create initial admin user
        await self.prompt_service.seed_default_prompts() # seed prompt templates from files
        self._initialized = True

    # ─── Phase 3: Async shutdown (called from FastAPI lifespan) ────────

    async def shutdown(self) -> None:
        for cache in self._client_caches:        # close all HTTP clients
            for client in cache.values():
                if hasattr(client, "aclose"):
                    await client.aclose()
        await self.postgres_store.shutdown()      # close connection pool
```

**Why two phases?** The constructor is synchronous (called during module import for CORS config). Database connections and seeding require `await`, so they happen in `initialize()`, which is called from FastAPI’s async lifespan handler.

**Key principle**: Notice that `RetrievalService` receives `vector_store` (the ABC type `VectorStore`), not `MilvusVectorStore`. The service has no idea it’s talking to Milvus. The container is the only place that knows.

### 10.2 `factories.py` — Config-Driven Lazy Instantiation

**Purpose**: Bridge between YAML config and registries. Given a model name like `"bge-m3"`, the factory looks up the config, resolves which implementation to use, creates the instance, and caches it.

**Concept**: This solves a problem that plain DI can’t — you don’t know at container construction time which embedder models will be requested. The retrieval pipeline for partition `"legal"` might use `"bge-m3"` while `"finance"` uses `"nomic-embed-text"`. The factory creates them on-demand when first requested.

```python
# di/factories.py

def make_component_factory(
    registry: Registry[T],                              # e.g. embedder_registry
    config_section: dict[str, ModelEndpointConfig],      # e.g. {"bge-m3": ModelEndpointConfig(...)}
    default_impl: str,                                   # e.g. "vllm"
    client_caches: list[dict[str, T]],                   # for shutdown cleanup
    extra_kwargs_fn: Callable | None = None,             # extract extra kwargs from config
) -> Callable[[str], T]:                                 # returns: factory(name) -> instance

    cache: dict[str, T] = {}
    lock = threading.Lock()
    client_caches.append(cache)                          # register cache for cleanup

    def factory(name: str) -> T:
        if name in cache:                                # fast path: already created
            return cache[name]
        with lock:                                       # thread-safe: double-checked locking
            if name in cache:
                return cache[name]

            model_cfg = config_section.get(name)         # lookup YAML config by name
            impl_name = model_cfg.extra.get(             # which implementation? "vllm" / "ollama"
                "implementation", default_impl
            )
            client = registry.create(impl_name,          # registry resolves class → instantiate
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                timeout=model_cfg.timeout,
            )
            cache[name] = client                         # cache singleton per model name
            return client

    return factory
```

**The resolution chain visualized:**

```
factory("bge-m3")                              # called by Retriever at query time
  │
  ├─► config.models.embedder["bge-m3"]         # YAML lookup
  │     endpoint: "http://ray-head:8000"
  │     model_name: "BAAI/bge-m3"
  │     extra: {implementation: "vllm"}
  │
  ├─► embedder_registry.create("vllm", ...)    # registry lookup → class resolution
  │     → VLLMEmbedder.__init__(endpoint=..., model_name=...)
  │
  └─► cache["bge-m3"] = instance               # cached — next call returns immediately
```

**Why not create all instances at startup?** Because:

1. Some models may never be requested (waste of connections)
2. Config can reference models that aren’t deployed yet
3. Lazy creation means faster startup
4. The cache provides singleton semantics per model name

**Why is the cache list registered in `client_caches`?** So the container can close all HTTP clients on shutdown — the factory creates `httpx.AsyncClient` instances that hold open connections and must be cleaned up.

### 10.3 `providers.py` — The FastAPI Bridge

**Purpose**: Expose container services to FastAPI route handlers via `Depends()`. This is the only file that the API layer imports from `di/`.

**Concept**: FastAPI’s `Depends()` system needs callable functions that return dependencies. Providers are thin accessor functions that reach into the singleton container and return the right service. They also enforce that the container has been fully initialized before any request is served.

```python
# di/providers.py

_container: ServiceContainer | None = None
_container_lock = threading.Lock()

def get_container() -> ServiceContainer:
    """Thread-safe singleton access to the container."""
    global _container
    if _container is None:
        with _container_lock:                    # double-checked locking
            if _container is None:
                _container = ServiceContainer()  # Phase 1: sync construction
    return _container

def _require_initialized() -> ServiceContainer:
    """Guard: reject requests before async Phase 2 completes."""
    c = get_container()
    if not c.is_initialized:
        raise RuntimeError("ServiceContainer.initialize() has not been called yet.")
    return c

# ─── One getter per service ───────────────────────────────────────

def get_retrieval_service() -> RetrievalService:
    return _require_initialized().retrieval_service

def get_document_service() -> DocumentService:
    return _require_initialized().document_service

def get_auth_service() -> AuthService:
    return _require_initialized().auth_service

# ... one per service (15 total)
```

**How it connects to routes:**

```python
# api/routers/user/retrieve.py

@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: RetrieveRequest,
    current_user: User = Depends(get_current_user),          # auth dependency
    service: RetrievalService = Depends(get_retrieval_service),  # service dependency
) -> RetrieveResponse:
    response = await service.retrieve(query=request.query, ...)
    return RetrieveResponse(...)
```

FastAPI calls `get_retrieval_service()` before entering the handler. The function reaches into the container singleton and returns the pre-wired `RetrievalService` instance. The route never knows how the service was constructed.

**Why not inject the container directly?** Because that would be the **Service Locator anti-pattern** — the route would depend on the entire container and could reach into any service. By injecting specific services, each route’s dependencies are visible in its function signature. This makes testing trivial:

```python
# In tests: override one service, not the whole container
app.dependency_overrides[get_retrieval_service] = lambda: mock_service
```

### 10.4 How the three files collaborate

```
                    Boot time                              Request time
                    ─────────                              ────────────

┌─────────────┐     ┌─────────────────────┐
│   YAML      │────►│   container.py      │
│   Config    │     │                     │
└─────────────┘     │  1. Register impls  │
                    │  2. Create adapters │
┌─────────────┐     │  3. Create factories│──── factories.py ────┐
│  Registries │◄────│  4. Wire services   │                      │
│  (7 typed)  │     └──────────┬──────────┘                      │
└─────────────┘                │                                 │
                               │ singleton                       │
                               ▼                                 │
                    ┌─────────────────────┐                      │
                    │   providers.py      │    factory("bge-m3") │
                    │                     │◄─────────────────────┘
                    │  get_container()    │    (lazy, on first request
                    │  get_*_service()    │     to that model name)
                    └──────────┬──────────┘
                               │ Depends()
                               ▼
                    ┌─────────────────────┐
                    │   FastAPI Route     │
                    │   (thin controller) │
                    └─────────────────────┘
```

### 10.5 Why this design avoids common DI anti-patterns

| Anti-pattern | How OpenRAG avoids it |
| --- | --- |
| **Service Locator** | Routes inject specific services via `Depends(get_retrieval_service)`, not the container. Each route’s dependencies are visible in its signature. |
| **Constructor over-injection** | Services receive only what they need. `RetrievalService` gets factories (`Callable`), not the container. |
| **Hidden dependencies** | No service calls `get_container()` internally. All dependencies are explicit constructor parameters. |
| **Lifecycle mismatch** | Two-phase init (sync constructor + async `initialize()`) separates what can be done at import time from what requires `await`. |
| **Leaked infrastructure** | Services receive `VectorStore` (ABC), not `MilvusVectorStore`. The concrete type is decided in one place: `container.py`. |
| **Untestable singletons** | The container accepts `config` as a constructor parameter. Tests pass mock config. `providers.py` has a module-level `_container` that tests patch before importing the app. |

---

## 11. Configuration Architecture

Two separate concerns, two separate locations:

### Runtime config data (YAML)

```
conf/
├── config.yaml               # Base: partitions, presets
├── api/dev.yaml              # Environment-specific API settings
├── api/prod.yaml
├── milvus/connection.yaml    # Milvus URI, index params
├── postgres/connection.yaml  # DB host, pool sizes
├── ray/cluster.yaml          # Ray cluster settings
└── auth/auth.yaml            # JWT secrets, token expiry
```

### Config schema (Python)

```python
# core/config/root.py

class OpenRAGConfig(BaseModel):
    models: ModelsConfig                          # embedder/llm/reranker/vlm endpoints
    partitions: dict[str, PartitionConfig]        # per-tenant config
    presets: PresetsConfig                        # pipeline templates
    milvus: MilvusConfig
    ray: RayConfig
    api: APIConfig
    postgres: PostgresConfig
    auth: AuthConfig
```

### Per-partition config

Every partition has its own pipeline configuration:

```python
# core/config/partition.py

class PartitionConfig(BaseModel):
    name: str
    indexation: IndexationPipelineConfig    # chunking strategy, embedder, VLM, etc.
    retrieval: RetrievalPipelineConfig      # pipeline type, embedder, reranker, LLM
    chat_history_depth: int = 0
    chat_llm: str | None = None
```

This means partition `"legal"` can use recursive chunking with contextualization enabled, while partition `"finance"` uses sentence chunking with HyDE retrieval — all from config, no code changes.

### Config loading with deep merge

```python
# core/config/loader.py

def load_config(conf_dir=None, overrides=None) -> OpenRAGConfig:
    base = _load_yaml(conf_dir / "config.yaml")
    for section in ("milvus", "ray", "api", "postgres", "s3", "auth"):
        section_cfg = _load_yaml(section_dir / env_file)
        base[section] = _deep_merge(base.get(section, {}), section_cfg)
    return OpenRAGConfig(**base)  # Pydantic validates everything
```

---

## 12. Flow 1: Document Indexing

```
User uploads PDF
  → API: POST /admin/indexing/document
    → file read + validate + upload to S3
    → job_service.create_batch_job(files, partition)
      → creates IndexationJob in Postgres
      → spawns background task
```

### The pipeline stages

The background task runs a sequential pipeline. The ordering is not arbitrary — each stage has a data dependency on the previous one:

```
  ┌─────────┐    ┌──────────┐    ┌─────────┐    ┌───────────┐    ┌────────────────┐    ┌─────────┐    ┌─────────┐
  │  PARSE  │ →  │ CAPTION  │ →  │  CHUNK  │ →  │ ENTITY    │ →  │ CONTEXTUALIZE  │ →  │  EMBED  │ →  │  STORE  │
  │ (Ray    │    │ (VLM     │    │ (CPU)   │    │ EXTRACT   │    │ (LLM HTTP)     │    │ (HTTP)  │    │ (PG +   │
  │  GPU)   │    │  HTTP)   │    │         │    │ (GLiNER)  │    │                │    │         │    │  Milvus)│
  └─────────┘    └──────────┘    └─────────┘    └───────────┘    └────────────────┘    └─────────┘    └─────────┘
  doc-level       doc-level       doc→chunks     chunk-level      chunk-level           chunk-level    chunk-level
```

### Why this exact order?

The stages are **sequential, not parallel**, because each depends on the output of the previous:

```python
# services/workers/pipeline_builder.py — the actual pipeline

async def run_post_parse_pipeline(parsed_rows, idx_config, ray_config):

    # 1. Caption BEFORE chunk — image descriptions must be in the markdown
    #    text so they end up inside the correct chunks after splitting
    if idx_config.enable_image_captioning:
        await caption_all(parsed_rows)              # doc-level: images → <image_description> tags

    # 2. Chunk — splits markdown into chunks. Must happen after captioning
    #    because caption text gets injected into the markdown first
    chunk_rows = await chunk_and_extract_metadata(parsed_rows)   # doc → N chunks

    # 3. Entity extraction — runs on chunk text, must happen after chunking
    if idx_config.enable_entity_extraction:
        extract_entities_all(chunk_rows)             # chunk-level: GLiNER NER

    # 4. Contextualize AFTER chunk — enriches each chunk with document context.
    #    Requires chunks to exist. Cannot run in parallel with captioning
    #    because captioning operates on doc-level data, not chunks.
    if idx_config.enable_contextualization:
        await contextualize_all(chunk_rows)          # chunk-level: LLM adds context preamble

    # 5. Embed — requires final chunk text (with context if added)
    await embed_all(chunk_rows)                      # chunk-level: text → vectors

    # 6. Store — requires both text and embeddings
    await store_all(chunk_rows)                      # dual-write: Postgres + Milvus
```

### Stage details

| Stage | Granularity | What it does | Key detail |
| --- | --- | --- | --- |
| **Parse** | doc → doc | PDF → Markdown via Ray Marker actors | GPU-accelerated, page-range chunking distributed across actors |
| **Caption** | doc → doc | Images → `<image_description>` tags in markdown | Optional. Runs BEFORE chunk so descriptions end up in the right chunks |
| **Chunk** | doc → N chunks | Markdown → Chunk objects | Registry-based: `chunking_registry.create(strategy)`. Also extracts doc-level metadata via optional LLM call |
| **Entity Extract** | chunk → chunk | Named entity recognition via GLiNER | Optional. Sync CPU, runs on chunk text |
| **Contextualize** | chunk → chunk | Adds LLM-generated context preamble | Optional. Runs AFTER chunk because it needs chunks to exist |
| **Embed** | chunk → chunk | Text → vectors via embedder HTTP API | Batched with semaphore-bounded concurrency |
| **Store** | chunk → chunk | Dual-write: text → Postgres, vectors → Milvus | Postgres-first ordering for data consistency |

### Data flow through stages

Each stage receives `list[dict]` and mutates rows in-place:

```python
# After parse:    row = {doc_id, text_blocks, images, partition, ...}
# After caption:  row = {doc_id, text_blocks (with <image_description>), images (with captions), ...}
# After chunk:    row = {chunk_id, doc_id, chunk_text, chunk_metadata, ...}  (1 doc → N rows)
# After context:  row = {chunk_id, doc_id, chunk_text (with context prepended), ...}
# After embed:    row = {chunk_id, doc_id, chunk_text, embedding: [0.12, -0.34, ...], ...}
# After store:    row = {chunk_id, doc_id, stored: True, ...}
```

Failed rows carry `_error` and `_error_stage` — subsequent stages skip them. This provides per-row fault isolation: one bad PDF doesn’t kill the batch.

---

## 13. Flow 2: Query Retrieval

```
RetrievalService.retrieve("What is RAG?", partition="legal")
  │
  ▼
Retriever.retrieve(query)
  │
  ├── get_partition_config("legal")
  │     → PartitionConfig.retrieval: type="multiquery", embedder="bge-m3", reranker="bge-reranker"
  │
  ├── embedder = embedder_factory("bge-m3")        → VLLMEmbedder (cached)
  ├── reranker = reranker_factory("bge-reranker")  → InfinityReranker (cached)
  ├── llm = llm_factory("mistral-small")           → VLLMClient (cached)
  │
  ▼
PipelineOrchestrator.execute(query, config, embedder, vector_store, reranker, llm)
  │
  ├── pipeline = pipeline_registry.create("multiquery")  → MultiQueryPipeline
  │
  ▼
MultiQueryPipeline.execute(query, embedder, vector_store, reranker, llm)
  │
  ├── 1. LLM generates 3 query variations
  ├── 2. Embed all 4 queries (original + 3 variations)
  ├── 3. Search Milvus 4 times (bounded concurrency, semaphore=8)
  ├── 4. Hydrate results from Postgres (batch fetch text by chunk IDs)
  ├── 5. Reciprocal Rank Fusion (merge + deduplicate + score)
  ├── 6. Rerank top candidates via cross-encoder
  │
  ▼
RetrievalResponse(results=[...], pipeline_used="multiquery", latency_ms=142.5)
```

### Chunk hydration: the two-store pattern

```
Milvus stores:  {id, embedding, document_id, partition, source_type, language, date_from, date_to}
Postgres stores: {id, document_id, partition, text, chunk_type, chunk_index, metadata}
```

Why? Text is large and doesn’t belong in a vector index. Milvus stores only what’s needed for vector search (embeddings + scalar filters). Text lives in Postgres.

After vector search, results are **hydrated** — chunk IDs from Milvus are batch-fetched from Postgres to get the actual text:

```python
# core/retrieval/hydration.py

async def hydrate_chunks(milvus_results, chunk_repo):
    chunk_ids = [r["id"] for r in milvus_results]
    pg_rows = await chunk_repo.get_by_ids(chunk_ids)      # single batch query
    text_map = {row["id"]: row for row in pg_rows}

    hydrated = []
    for r in milvus_results:                               # preserve score ordering
        pg_data = text_map.get(r["id"])
        if pg_data is None:
            logger.warning("orphaned_milvus_vector", ...)
            continue
        hydrated.append({**r, **pg_data})
    return hydrated
```

---

## 14. Flow 3: Chat Completion (OpenAI-compatible)

```
POST /v1/chat/completions
  {"model": "legal", "messages": [...], "stream": true}
    │
    ▼
  chat.py (thin controller)
    │
    ├── Extract user query from messages
    ├── Resolve system prompt (user-provided → DB → default)
    ├── Extract chat history: extract_chat_history(messages, depth)
    │
    ▼
  QueryService.execute(query, partition="legal", history=...)
    │
    ├── QueryRewriter: rewrite query using conversation context
    ├── QueryPlanner: classify intent (qa / summarization / analytics / enumeration)
    ├── QueryOrchestrator: dispatch by intent
    │     ├── QA → multiquery pipeline + entity merge
    │     ├── Summarization → find document, return all chunks
    │     ├── Analytics → HyDE pipeline with high top_k
    │     └── Enumeration → simple pipeline with list-oriented prompt
    │
    ▼
  Build RAG prompt: system_prompt + history + context + question
    │
    ▼
  LLM.stream_chat(messages) → SSE tokens → client

  Response format (OpenAI-compatible):
  data: {"choices": [{"delta": {"content": "RAG is..."}}]}
  data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}
  data: [DONE]
```

---

## 15. Flow 4: Adding a New Component

### Example: Add Cohere as an embedding provider

**Step 1** — Create implementation (1 new file):

```python
# services/inference/cohere_client.py

@embedder_registry.register("cohere")
class CohereEmbedder(Embedder):
    def __init__(self, endpoint: str, model_name: str, batch_size: int = 32, **kwargs):
        self._client = httpx.AsyncClient(...)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Cohere API call
        ...
```

**Step 2** — Register at boot (1 line):

```python
# di/embedders.py
def register_embedders():
    import openrag.services.inference.vllm_client
    import openrag.services.inference.ollama_client
    import openrag.services.inference.cohere_client   # ← add this
```

**Step 3** — Configure in YAML (config only):

```yaml
# conf/config.yaml
models:
embedder:
cohere-embed-v4:
endpoint: https://api.cohere.com
model_name: embed-multilingual-v3.0
extra:
implementation: cohere
api_key: ${COHERE_API_KEY}
```

**That’s it.** Zero changes to core, DI container, retrieval service, or any pipeline. The `Retriever` calls `embedder_factory("cohere-embed-v4")` and gets a `CohereEmbedder`.

### Similarly: Add a new retrieval pipeline

```python
# core/retrieval/pipelines/my_custom.py

@pipeline_registry.register("custom")
class CustomPipeline(RetrievalPipeline):
    async def execute(self, query, embedder, vector_store, **kwargs):
        # Your custom retrieval logic
        ...
```

Then set `retrieval.type: "custom"` in any partition’s config.

---

## 16. Multi-Tenancy

Each partition is an isolated namespace with its own:

- Chunking strategy (markdown_layout / recursive / sentence)
- Embedding model (bge-m3 / nomic-embed-text / cohere)
- Retrieval pipeline (simple / multiquery / HyDE)
- Reranking model
- LLM for chat
- Prompt templates (contextualization, metadata extraction, system prompts)
- Image captioning toggle
- Contextualization toggle

```yaml
partitions:
legal:
indexation:
preset: legal                     # recursive chunking, contextualization ON
retrieval:
type: multiquery
embedder: bge-m3
reranker: bge-reranker
llm: mistral-small
num_queries:3

finance:
indexation:
preset: finance                   # sentence chunking, smaller chunks
retrieval:
type: hyde
embedder: bge-m3
reranker: bge-reranker
llm: mistral-small
```

The retriever resolves the partition config at query time:

```python
# core/retrieval/retriever.py

async def retrieve(self, query: RetrievalQuery):
    partition_cfg = get_partition_config(self._config, query.partition)
    pipeline_cfg = partition_cfg.retrieval

    embedder = self._embedder_factory(pipeline_cfg.embedder)   # resolved per partition
    pipeline = pipeline_registry.create(pipeline_cfg.type)      # resolved per partition
```

---

## 17. Observability Architecture

### Metrics (Prometheus)

22 metrics defined in `core/observability/metrics.py`:

| Metric | Type | Label |
| --- | --- | --- |
| `openrag_retrieval_requests_total` | Counter | partition, pipeline |
| `openrag_retrieval_latency_seconds` | Histogram | partition, pipeline |
| `openrag_retrieval_results_count` | Histogram | partition |
| `openrag_embedding_requests_total` | Counter | model |
| `openrag_embedding_latency_seconds` | Histogram | model |
| `openrag_storage_operation_latency` | Histogram | backend, operation |
| `openrag_documents_indexed_total` | Counter | partition |
| `openrag_chunks_created_total` | Counter | partition |
| `openrag_worker_task_failures_total` | Counter | stage |
| `openrag_data_loss_events_total` | Counter | partition |
| `openrag_auth_failures_total` | Counter | reason |

### Tracing (per-pipeline)

```python
# core/utils/tracing.py

class PipelineTrace:
    @contextmanager
    def span(self, name: str, **metadata):
        record = SpanRecord(name=name, start_time=time.monotonic(), metadata=metadata)
        try:
            yield record
        finally:
            record.end_time = time.monotonic()
            self.spans.append(record)
```

Used in pipelines:

```python
with trace.span("embed_query"):
    query_embedding = await embedder.embed_single(query.text)

with trace.span("vector_search", top_k=query.top_k):
    raw_results = await vector_store.search(...)
```

### Structured logging (structlog)

```python
logger.info(
    "retrieval_completed",
    pipeline="multiquery",
    partition="legal",
    results=5,
    latency_ms=142.5,
    trace=trace.summary(),
)
```

---

## 18. Key Takeaways

### 1. The dependency rule is non-negotiable

`core ← services ← api`. Verify with `grep`. No exceptions.

### 2. ABCs are your swap points

Every infrastructure component has an ABC in core. Swapping Milvus for Qdrant is one new file + one DI wiring change.

### 3. Registry + Factory = plugin system

Self-registering decorators + config-driven factory = add new providers without touching existing code. True Open/Closed Principle.

### 4. Config is data, schema is code

YAML files in `conf/` (deployment artifacts). Pydantic models in `core/config/` (validated schema). They meet at the loader.

### 5. Per-partition everything

Every tenant gets their own chunking, embedding, retrieval, and prompting configuration. The same codebase serves different use cases.

### 6. Thin controllers, fat services

Routes read requests, call services, map responses. Business logic lives in `services/orchestrators/`. Domain logic lives in `core/`.

### 7. Metrics are definitions, not implementations

`core/observability/metrics.py` defines counters and histograms. They’re incremented at orchestrator/pipeline level. No metrics code in low-level utilities.

### 8. Test the core without infrastructure

Because `core` has zero infrastructure imports, you can unit test chunking, retrieval pipelines, config loading, and domain models without Postgres, Milvus, or Ray running.

---

## Architecture at a Glance

```
                    ┌──────────────────┐
                    │   YAML Config    │
                    │   conf/*.yaml    │
                    └────────┬─────────┘
                             │ load + validate
                             ▼
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Registries │◄───│  DI Container    │───►│  Component      │
│  (7 typed)  │    │  (wires all)     │    │  Factories      │
└──────┬──────┘    └────────┬─────────┘    │  (cached, lazy) │
       │                    │              └────────┬────────┘
       │         ┌──────────┴──────────┐            │
       │         │                     │            │
       ▼         ▼                     ▼            ▼
┌────────────┐ ┌──────────────┐ ┌────────────┐ ┌──────────────┐
│ Retrieval  │ │ Indexing     │ │ API Routes │ │ Inference    │
│ Pipelines  │ │ Pipeline     │ │ (thin)     │ │ Clients      │
│ (strategy) │ │ (Ray stages) │ │            │ │ (HTTP)       │
└─────┬──────┘ └──────┬───────┘ └────────────┘ └──────────────┘
      │               │
      ▼               ▼
┌────────────┐ ┌──────────────┐
│ VectorStore│ │ Postgres     │
│ (Milvus)   │ │ Repositories │
└────────────┘ └──────────────┘
```