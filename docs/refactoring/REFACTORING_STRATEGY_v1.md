# OpenRAG → Hexagonal Architecture Refactoring Strategy (v2)

> **Constraint:** After every commit the system MUST be deployable and pass existing tests.
> **Method:** Strangler Fig — new structure grows alongside old; re-exports keep old
> import paths alive until every consumer has migrated; only then are re-exports removed.

---

## Table of Contents

1. [Current vs Target Assessment](#1-current-vs-target-assessment)
2. [Key Design Patterns](#2-key-design-patterns)
3. [Target Architecture](#3-target-architecture)
4. [Guiding Principles](#4-guiding-principles)
5. [Phase Overview](#5-phase-overview)
6. [Phase 0 — Scaffold & Import Guard](#phase-0--scaffold--import-guard)
7. [Phase 1 — Generic Registry & Exceptions](#phase-1--generic-registry--exceptions)
8. [Phase 2 — Domain Models](#phase-2--domain-models)
9. [Phase 3 — Configuration Schemas](#phase-3--configuration-schemas)
10. [Phase 4 — ABCs & Ports](#phase-4--interfaces--ports)
11. [Phase 5 — Core Domain Logic](#phase-5--core-domain-logic)
12. [Phase 6 — Inference Adapters](#phase-6--inference-adapters)
13. [Phase 7 — Storage & Persistence Adapters](#phase-7--storage--persistence-adapters)
14. [Phase 8 — Orchestrators](#phase-8--orchestrators)
15. [Phase 9 — Workers (Ray Isolation)](#phase-9--workers-ray-isolation)
16. [Phase 10 — API Layer Restructure](#phase-10--api-layer-restructure)
17. [Phase 11 — Composition Root (DI)](#phase-11--composition-root-di)
18. [Phase 12 — Internal Cleanup & Remove Shims](#phase-12--internal-cleanup--remove-shims)
19. [Phase 13 — Project Layout, Infra, Tests & UI](#phase-13--project-layout-infra-tests--ui)
20. [Phase 14 — Per-Partition Presets (Indexation & Retrieval)](#phase-14--per-partition-presets-indexation--retrieval)
21. [Phase 15 — OIDC / Keycloak SSO Authentication](#phase-15--oidc--keycloak-sso-authentication)
22. [Risk Register](#risk-register)
23. [Migration Utilities](#migration-utilities)

---

## 1. Current vs Target Assessment

### Current codebase (openrag 1.1.8)

| Metric                  | Value                                                                     |
| ----------------------- | ------------------------------------------------------------------------- |
| Production Python files | 105                                                                       |
| Total LOC (non-test)    | ~15,824                                                                   |
| Package                 | `openrag/` with `components/`, `routers/`, `config/`, `models/`, `utils/` |
| External infra          | Milvus, PostgreSQL, Ray, vLLM/Ollama                                      |

### Coupling hotspots

| Hotspot                                                                                                         | Symptom                                                  | Fix                                                                                                             |
| --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Global `config = load_config()` at module level** (12+ files)                                                 | Cannot test/compose without full config                  | `ServiceContainer` loads config once, passes via constructor                                                    |
| **Ray actor singletons at import time** (`utils/dependencies.py`)                                               | Importing any router boots Ray + Milvus + GPU semaphores | Lazy init in `container.initialize()`, actors created only when needed                                          |
| **LangChain `Document` as universal data type**                                                                 | Domain tied to third-party class                         | Domain `Chunk` + `Document` models with `from_langchain()` converters                                           |
| **Vectordb actor = God object** (Milvus + PG + embedding + users + partitions + workspaces)                     | Single point of failure, untestable                      | Split into `MilvusVectorStore`, `PostgresStore` with 13 repos, `Embedder` port                                  |
| **No interface boundaries** for LLM, VectorDB, persistence                                                      | Cannot swap, mock, or test                               | ABCs co-located in subject folders (`core/embeddings/embedder.py`, etc.) + 13 repository ports in `core/ports/` |
| **Per-component Factory classes** (`RetrieverFactory`, `RerankerFactory`, `ChunkerFactory`, `EmbeddingFactory`) | Duplicated pattern, no DI                                | Single `Registry[T]` generic + `make_component_factory()` for config-driven instantiation                       |
| **Router-to-component direct imports**                                                                          | API fused to business logic                              | Orchestrator services injected via `Depends(get_service)`                                                       |

### Existing good patterns to preserve

| Pattern                                         | Location                             | Action                                                                 |
| ----------------------------------------------- | ------------------------------------ | ---------------------------------------------------------------------- |
| Retriever strategies (Single, MultiQuery, HyDE) | `components/retriever.py`            | Move to `core/retrieval/`, register in `retriever_registry`            |
| BaseReranker ABC + factory                      | `components/reranker/`               | Promote to `core/rerankers/reranker.py` + `core/rerankers/registry.py` |
| BaseChunker + RecursiveSplitter                 | `components/indexer/chunker/`        | Move to `core/chunking/`                                               |
| BaseEmbedding + OpenAIEmbedding                 | `components/indexer/embeddings/`     | Promote to `core/embeddings/embedder.py`                               |
| BaseLoader ABC for file parsers                 | `components/indexer/loaders/base.py` | Promote to `core/indexing/parsers/document_parser.py`                  |
| Pydantic config models (frozen)                 | `config/models.py`                   | Split into `core/config/` sections                                     |
| Custom exception hierarchy                      | `utils/exceptions/`                  | Consolidate in `core/utils/exceptions.py`                              |
| Web search provider abstraction                 | `components/websearch/base.py`       | Keep as pluggable adapter                                              |
| Disk-loaded prompt templates                    | `components/prompts/`                | Move to `openrag/prompts/`                                             |

---

## 2. Key Design Patterns

### 2.1 Registry[T] — The plugin pattern core

```python
class Registry(Generic[T]):
    def __init__(self, kind: str) -> None
    def register(self, name: str) -> Callable[[Type[T]], Type[T]]  # decorator
    def create(self, name: str, **kwargs: Any) -> T
    def get_class(self, name: str) -> Type[T]
    def list_registered(self) -> list[str]
    def __contains__(self, name: str) -> bool
```

**Key insight:** Implementations register via decorator on class definition:

```python
@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder):
    ...
```

Registration happens via side-effect imports in `di/embedders.py`:

```python
def register_embedders() -> None:
    import openrag.services.inference.vllm_client    # noqa: F401
    import openrag.services.inference.ollama_client  # noqa: F401
```

**Replaces** in OpenRAG: `RetrieverFactory`, `RerankerFactory`, `ChunkerFactory`,
`EmbeddingFactory`, `WebSearchFactory` — all five become `Registry[T]` instances.

### 2.2 make_component_factory() — Config-driven, cached, thread-safe

```python
def make_component_factory(
    registry: Registry[T],
    config_section: dict[str, ModelEndpointConfig],
    default_impl: str,
    client_caches: list[dict[str, T]],
    extra_kwargs_fn: Callable | None = None,
) -> Callable[[str], T]:
```

**Key insight:** Returns a `Callable[[str], T]` that orchestrators receive instead of
concrete instances. First call creates + caches; subsequent calls return cached.
Thread-safe with double-checked locking. Cache dict appended to `client_caches` for
lifecycle cleanup in `ServiceContainer.shutdown()`.

**Replaces** in OpenRAG: Module-level singletons like `ragpipe = RagPipeline()`.

### 2.3 ServiceContainer — Composition root with sync/async split

**Lifecycle:**

1. **`__init__` (sync):** Load config → register impls → create stores → create factories → create services
2. **`initialize()` (async):** Open DB pool → run migrations → seed defaults → mark initialized
3. **`shutdown()` (async):** Close HTTP clients → close DB pool

**Key insight:** Sync init creates the entire object graph (no I/O). Async init
does all the actual connecting. This means `__init__` can't fail on network issues.

### 2.4 Co-located ABCs — ABC lives with its subject folder

Each ABC is co-located inside the subject folder that owns it. This keeps each domain concept
self-contained — the ABC, its registry, and its implementations all discoverable in
one place.

**Pattern:** Each subject folder owns its ABC + registry + `__init__.py` re-exports:

```
core/embeddings/
    embedder.py             # Embedder ABC
    registry.py             # embedder_registry: Registry[Embedder]
    __init__.py             # re-exports Embedder + embedder_registry
```

Consumers import cleanly:

```python
from openrag.core.embeddings import Embedder, embedder_registry
```

**Component ABCs** (co-located in subject folders):

- `core/embeddings/embedder.py` — Embedder ABC
- `core/rerankers/reranker.py` — Reranker ABC
- `core/llm/llm.py` — LLM ABC
- `core/vlm/vlm.py` — VLM ABC
- `core/vector_stores/vector_store.py` — VectorStore ABC
- `core/chunking/chunking_strategy.py` — ChunkingStrategy ABC
- `core/indexing/parsers/document_parser.py` — DocumentParser ABC

**Repository ports** (CRUD contracts + CatalogStore aggregate root):

- `core/ports/catalog_store.py` — CatalogStore ABC (composes all repos)
- `core/ports/` — DocumentRepository, ChunkRepository, UserRepository, etc.

**Key insight:** Don't put VectorStore in `ports/`. It's a pluggable component
(searchable, swappable backends), not a CRUD repo.

### 2.5 CatalogStore — Composite repository pattern

```python
class PostgresStore(CatalogStore):
    def __init__(self, config: PostgresConfig):
        self._conn = ConnectionManager(config)
        pool_getter = lambda: self._conn.pool  # lazy: repos don't touch pool until init
        self._documents = PgDocumentRepository(pool_getter)
        self._users = PgUserRepository(pool_getter)
        # ... 13 repos total

    @property
    def document_repo(self) -> DocumentRepository: return self._documents
```

**Key insight:** Single `ConnectionManager` owns the pool. Repos receive a `pool_getter`
lambda so they can defer access until the pool is live. Composition, not inheritance.

**Replaces** in OpenRAG: The monolithic `PartitionFileManager` (currently in
`vectordb/utils.py`) which does file + user + partition + workspace + membership ops
through a single class.

### 2.6 Thin Ray actor wrappers

```python
@ray.remote(max_concurrency=2, max_restarts=3)
class IndexerActorClass:
    def __init__(self):
        # Only imports needed for registry registration
        import openrag.services.inference.vllm_client  # noqa
        ...

    async def process_document(self, doc_dict, config_dict, prompt_overrides, job_id):
        document = Document.model_validate(doc_dict)
        config = Settings.model_validate(config_dict)
        result = await ray_data_ingest_documents([document], config, ...)
        return {"status": "success", **result.successful[0]}
```

**Key insight:** The actor is just serialization boundary + error boundary. All logic
@ray.remote(max_concurrency=2, max_restarts=3)
class IndexerActorClass:
    def __init__(self):
        # Only imports needed for registry registration
        import openrag.services.inference.vllm_client  # noqa
        ...

    async def process_document(self, doc_dict, config_dict, prompt_overrides, job_id):
        document = Document.model_validate(doc_dict)
        config = Settings.model_validate(config_dict)
        result = await ray_data_ingest_documents([document], config, ...)
        return {"status": "success", **result.successful[0]}@ray.remote(max_concurrency=2, max_restarts=3)
    class IndexerActorClass:
        def __init__(self):
            # Only imports needed for registry registration
            import openrag.services.inference.vllm_client  # noqa
            ...
    
        async def process_document(self, doc_dict, config_dict, prompt_overrides, job_id):
            document = Document.model_validate(doc_dict)
            config = Settings.model_validate(config_dict)
            result = await ray_data_ingest_documents([document], config, ...)
            return {"status": "success", **result.successful[0]}lives in the indexing service/pipeline. Models are serialized to dicts by caller,
    deserialized by actor. No state between calls.

**Replaces** in OpenRAG: The 300-line `Indexer` actor that mixes chunking, embedding,
task state management, and file cleanup.

### 2.8 Pipeline stages as modules

Each stage is a pure async function. `pipeline_builder.py` chains them sequentially
with per-stage timeout, error marking, and credential scrubbing.

**Key patterns:**

- Rows are mutated in-place (no functional pipeline overhead)
- Failed rows get `_error` field (informational, still passed to next stage)
- Credentials scrubbed after the stage that needs them
- Timeout: base + per-chunk scaling (except contextualize — no stage timeout to prevent cascade)

### 2.9 All-async ABCs

All component ABCs should be async-native:

```python
class Embedder(ABC):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_single(self, text: str) -> list[float]: ...
```

The current `BaseEmbedding` inherits LangChain's
`Embeddings` class (sync `embed_documents` + async wrappers via thread pool). The new
`Embedder` ABC should be async-native.

### 2.10 Structured error responses

Error responses should be structured with request tracing:

```json
{
  "error": {
    "message": "Document not found",
    "type": "not_found_error",
    "code": "NOT_FOUND",
    "request_id": "req_a1b2c3d4..."
  }
}
```

Domain exceptions walk the MRO to find the correct HTTP status code. Request ID
comes from middleware (structlog contextvars).

### 2.11 Per-partition pipeline configuration

Each partition can have its own `IndexationPipelineConfig` and `RetrievalPipelineConfig`.
Presets are reusable named configs stored in DB. Partitions reference presets by name.

Currently OpenRAG has global config only. Adding per-partition
config is a Phase 14 enhancement, not part of the initial refactor.

# 

### 2.13 BM25 approach

An alternative to Milvus BM25 is duplicating chunk text in Postgres (with tsvector index)
and using `ts_rank` scoring for full-text search, while keeping Milvus for dense vectors only.
OpenRAG uses Milvus's built-in BM25 (sparse vectors). Both approaches work; OpenRAG's
is more Milvus-native and avoids data duplication.

---

## 3. Target Architecture

```
+-----------------------------------------------------------------+
|                        API Layer                                |
|  FastAPI routers, middleware, Pydantic request/response          |
|  schemas, auth dependencies, error handlers                     |
|                                                                 |
|  openrag/api/                                                   |
+-----------------------------------------------------------------+
|                     Services Layer                              |
|  Adapter implementations, orchestrators, Ray workers,           |
|  inference clients, storage backends, persistence repos         |
|                                                                 |
|  openrag/services/                                              |
+-----------------------------------------------------------------+
|                      Core Layer                                 |
|  Domain models, interface ABCs, port ABCs, config schemas,      |
|  retrieval algorithms, chunking strategies, registry,           |
|  exceptions, observability definitions                          |
|                                                                 |
|  openrag/core/      (PURE - no infra imports)                   |
+-----------------------------------------------------------------+
|                   Composition Root                              |
|  ServiceContainer, Depends() providers, registry wiring,        |
|  config-driven factories                                        |
|                                                                 |
|  openrag/di/                                                    |
+-----------------------------------------------------------------+
```

### Dependency rule (STRICT)

```
api/ --> di/ --> services/ --> core/
api/ --> core/ (models, config, ABCs - read-only)
services/ --> core/
core/ --> (nothing in openrag - only stdlib + pure libs like pydantic)
di/ --> core/ + services/ (the ONLY place that crosses boundaries)
```

### Directory structure (full)

```
openrag/
|-- core/
|   |-- config/                  # Typed config schemas + YAML loader
|   |   |-- auth.py              # AuthConfig + OIDCConfig (issuer, client_id, claim mapping)
|   |   |-- endpoints.py         # ModelEndpointConfig + ModelsConfig (embedder, reranker, llm, vlm)
|   |   |-- chunking.py, indexation.py, infrastructure.py,
|   |   |   loader.py, partition.py, presets.py, retrieval.py, root.py
|   |   +-- __init__.py
|   |
|   |-- ports/                   # Repository contracts + CatalogStore aggregate root
|   |   |-- catalog_store.py     # CatalogStore ABC - composes all repos, owns pool lifecycle
|   |   |-- document_repo.py, chunk_repo.py, user_repo.py, job_repo.py,
|   |   |   partition_repo.py, conversation_repo.py, prompt_repo.py,
|   |   |   entity_repo.py, topic_tag_repo.py, audit_log_repo.py,
|   |   |   idempotency_repo.py, model_endpoint_repo.py, preset_repo.py
|   |   +-- __init__.py
|   |
|   |-- models/                  # Domain entities (Pydantic frozen models)
|   |   |-- catalog.py           # DocumentRecord, IndexationJob, status enums
|   |   |-- chunk.py             # Chunk, ChunkType enum
|   |   |-- contextualization.py # ContextualizedQuery
|   |   |-- conversation.py      # Conversation, Message
|   |   |-- document.py          # Document, ProcessedDocument, TextBlock, ImageBlock
|   |   |-- prompt.py            # Prompt, PromptType enum
|   |   |-- query.py             # RetrievalQuery
|   |   |-- retrieval_response.py # RetrievalResponse, ScoredChunk
|   |   |-- retrieval_result.py  # RetrievalResult
|   |   +-- user.py              # User, SystemRole, PartitionRole
|   |
|   |-- chunking/                # ABC + strategies + registry
|   |   |-- __init__.py          # re-exports ChunkingStrategy + chunking_registry
|   |   |-- chunking_strategy.py # ChunkingStrategy ABC
|   |   |-- registry.py          # chunking_registry: Registry[ChunkingStrategy]
|   |   |-- recursive.py, fixed.py, sentence.py,
|   |   |   markdown_section.py, markdown_layout.py
|   |
|   |-- embeddings/              # ABC + registry (impls in services/inference/)
|   |   |-- __init__.py          # re-exports Embedder + embedder_registry
|   |   |-- embedder.py          # Embedder ABC - async embed/embed_single/dimension
|   |   +-- registry.py          # embedder_registry: Registry[Embedder]
|   |
|   |-- rerankers/               # ABC + registry
|   |   |-- __init__.py          # re-exports Reranker + reranker_registry
|   |   |-- reranker.py          # Reranker ABC - rerank(query, docs, top_k)
|   |   +-- registry.py          # reranker_registry: Registry[Reranker]
|   |
|   |-- llm/                     # ABC + registry only
|   |   |-- __init__.py          # re-exports LLM + llm_registry
|   |   |-- llm.py               # LLM ABC - generate/chat/stream_chat/chat_with_tools
|   |   +-- registry.py          # llm_registry: Registry[LLM]
|   |
|   |-- vlm/                     # ABC + registry
|   |   |-- __init__.py          # re-exports VLM + vlm_registry
|   |   |-- vlm.py               # VLM ABC - caption_image/caption_images_batch
|   |   +-- registry.py          # vlm_registry: Registry[VLM]
|   |
|   |-- prompts/                 # Prompt assembly logic (all builders in one place)
|   |   |-- __init__.py
|   |   |-- chat_prompt_builder.py       # RAG chat: context + system prompt + query -> messages
|   |   |-- vlm_prompt_builder.py        # VLM: image + caption template -> messages
|   |   |-- contextualization_builder.py # chunk contextualization prompt assembly
|   |   |-- query_rewriter.py            # multi-query / HyDE prompt building
|   |   |-- map_reduce_builder.py        # map / reduce per-chunk prompts
|   |   +-- template_loader.py           # load_template(name) -> str (reads from openrag/prompts/templates/)
|   |
|   |-- vector_stores/           # ABC (impls in services/storage/)
|   |   |-- __init__.py          # re-exports VectorStore
|   |   +-- vector_store.py      # VectorStore ABC - upsert/search/delete/ensure_collection
|   |
|   |-- retrieval/               # Retrieval algorithms (the RAG core)
|   |   |-- pipeline.py          # UnifiedPipeline (dense + BM25 + entity + RRF)
|   |   |-- retriever.py         # Retriever facade
|   |   |-- entity_retrieval.py
|   |   |-- hydration.py
|   |   +-- rrf.py               # Reciprocal Rank Fusion (pure math)
|   |
|   |-- indexing/                # Document ingestion domain logic
|   |   |-- contextualize.py, text_preprocessor.py, image_preprocessor.py,
|   |   |   validators.py
|   |   +-- parsers/
|   |       |-- __init__.py      # re-exports DocumentParser + parser_registry
|   |       |-- document_parser.py # DocumentParser ABC - parse(document)
|   |       |-- registry.py      # parser_registry: Registry[DocumentParser]
|   |       |-- text_parser.py, html_parser.py, pdf_parser.py,
|   |       |   image_parser.py, audio_parser.py, video_parser.py
|   |
|   |-- observability/
|   |   +-- metrics.py           # Prometheus definitions only
|   |
|   +-- utils/
|       |-- registry.py          # Registry[T] - THE plugin pattern core
|       |-- exceptions.py        # OpenRAGError hierarchy
|       |-- text.py, dates.py, filename.py, mime_validation.py,
|       |   logging.py, retry.py, streaming.py, tracing.py,
|       |   debug.py, scrub.py
|       +-- __init__.py
|
|-- services/
|   |-- auth/                    # Authentication adapters
|   |   |-- jwt_validator.py     # JWKS-based JWT signature verification
|   |   |-- oidc_mapper.py       # JWT claims -> OpenRAG User + partition roles
|   |   +-- oidc_provisioner.py  # Auto-create/sync users from OIDC claims
|   |
|   |-- inference/               # HTTP clients to inference services
|   |   |-- vllm_client.py       # @embedder_registry.register("vllm"), @llm_registry.register("vllm")
|   |   |-- ollama_client.py     # @embedder_registry.register("ollama"), @llm_registry.register("ollama")
|   |   |-- infinity_client.py   # @reranker_registry.register("infinity")
|   |   |-- vlm_client.py        # @vlm_registry.register("vllm")
|   |   |-- healthcheck.py
|   |   |-- _circuit_breaker.py  # @with_circuit_breaker decorator (aiobreaker)
|   |   |-- _retry.py            # @with_retry decorator (tenacity + jitter)
|   |   |-- _timeout.py          # @with_timeout decorator (asyncio.timeout)
|   |   +-- distributed_semaphore.py
|   |
|   |-- storage/
|   |   |-- postgres_store.py    # PostgresStore implements CatalogStore (composite of 13 repos)
|   |   |-- milvus_store.py      # MilvusVectorStore implements VectorStore
|   |   +-- s3_store.py          # S3Store - document upload/download
|   |
|   |-- persistence/             # Postgres repository implementations
|   |   |-- connection.py        # ConnectionManager (asyncpg pool lifecycle + retry)
|   |   |-- schema.py            # SQLAlchemy metadata (all tables)
|   |   |-- document_repo.py, chunk_repo.py, job_repo.py, user_repo.py,
|   |   |   partition_repo.py, prompt_repo.py, conversation_repo.py,
|   |   |   entity_repo.py, topic_tag_repo.py, audit_log_repo.py,
|   |   |   idempotency_repo.py, model_endpoint_repo.py, preset_repo.py
|   |   +-- migrations/
|   |       |-- env.py
|   |       +-- versions/        # append-only migration history
|   |
|   |-- orchestrators/           # Business services (high-level flows)
|   |   |-- retrieval_service.py, indexing_service.py, document_service.py,
|   |   |   job_service.py, partition_service.py, query_service.py,
|   |   |   query_orchestrator.py, auth_service.py, user_service.py,
|   |   |   prompt_service.py, model_endpoint_service.py, preset_service.py,
|   |   |   cluster_service.py, conversation_service.py, conversion_service.py,
|   |   |   llm_contextualizer.py, research_planner.py
|   |   +-- __init__.py
|   |
|   |-- workers/                 # Ray-based distributed workers
|   |   |-- ray_utils.py, pipeline_builder.py, batch_ingest.py,
|   |   |   ray_data_ingest.py, indexer_actor.py, result_aggregation.py
|   |   +-- stages/
|   |       |-- parse.py, caption.py, chunk.py, contextualize.py,
|   |       |   embed.py, store.py
|   |       +-- __init__.py
|   |
|   +-- events/
|       +-- job_events.py        # In-process SSE event bus
|
|-- api/
|   |-- main.py                  # FastAPI app, lifespan, middleware, routes
|   |-- error_handlers.py        # Domain exception -> JSON response
|   |
|   |-- dependencies/
|   |   |-- auth.py              # Dual auth: OIDC JWT + API token, RBAC
|   |   |-- audit.py
|   |   +-- rate_limit.py
|   |
|   |-- middleware/
|   |   |-- request_id.py, idempotency.py, request_timeout.py,
|   |   |   security_headers.py, instrumentation.py
|   |   +-- __init__.py
|   |
|   |-- routers/
|   |   |-- auth/login.py
|   |   |-- user/
|   |   |   |-- chat.py, retrieve.py, query_plan.py, health.py,
|   |   |   |   partitions.py, me.py, account.py, documents.py,
|   |   |   |   chat_conversations.py
|   |   |   +-- __init__.py
|   |   +-- admin/
|   |       |-- indexing.py, partitions.py, pipelines.py, documents.py,
|   |       |   jobs.py, prompts.py, model_endpoints.py, presets.py,
|   |       |   system.py, convert.py, users.py, audit_log.py
|   |       +-- __init__.py
|   |
|   +-- schemas/
|       |-- user/, admin/, auth/
|       +-- __init__.py
|
|-- di/
|   |-- container.py             # ServiceContainer - composition root
|   |-- providers.py             # FastAPI Depends() accessors
|   |-- factories.py             # make_component_factory() - config-driven cached factory
|   |-- embedders.py             # register_embedders() - side-effect imports
|   |-- rerankers.py             # register_rerankers()
|   |-- llms.py                  # register_llms()
|   |-- vlms.py                  # register_vlms()
|   |-- vector_stores.py         # create_vector_store()
|   +-- repositories.py          # create_catalog_store()
|
+-- prompts/                     # Disk-loaded prompt templates
    +-- templates/
```

---

## 4. Guiding Principles

### 4.1 Strangler Fig migration

Every module move follows this pattern:

```
Commit A: Create new file at target location
Commit B: Update old file to re-export from new location
Commit C: Update consumers to import from new location
... Phase 12: Delete old re-export shim
```

### 4.2 Co-located ABCs in subject folders, ports/ for CRUD repos

Each component ABC lives inside its subject folder alongside its registry.
The folder's `__init__.py` re-exports the public surface for clean imports:

```python
# core/embeddings/__init__.py
from openrag.core.embeddings.embedder import Embedder
from openrag.core.embeddings.registry import embedder_registry
__all__ = ["Embedder", "embedder_registry"]

# Consumer code:
from openrag.core.embeddings import Embedder, embedder_registry
```

Same pattern for `rerankers/`, `llm/`, `vlm/`, `chunking/`, `vector_stores/`,
and `indexing/parsers/`.

Ports (`core/ports/`) hold CRUD repository ABCs + `CatalogStore` (the aggregate root
that composes all repos).

### 4.3 Config injection via ServiceContainer

Replace `config = load_config()` at module level with constructor injection.
`ServiceContainer.__init__()` is the only place that calls `load_config()`.
Services receive typed config sections, not the entire Settings object.

### 4.4 Factory callables over concrete instances

Orchestrators receive `Callable[[str], Embedder]` (factory), not `Embedder` (instance).
This enables lazy creation, model switching, and lifecycle management.

**Pattern:**

```python
class RetrievalService:
    def __init__(
        self,
        vector_store: VectorStore,
        embedder_factory: Callable[[str], Embedder],   # NOT Embedder
        reranker_factory: Callable[[str], Reranker],
        ...
    ): ...
```

### 4.5 Ray is an infrastructure detail

Ray actors in `services/workers/` are thin wrappers. They serialize/deserialize
domain models (dict round-trip), call service methods, and return results.
Core domain logic works without Ray.

### 4.6 Async-native interfaces

All interface ABCs are async. No inheriting from LangChain's sync `Embeddings` class.
Sync operations wrapped in `asyncio.to_thread()` at the adapter level.

### 4.7 Domain models replace LangChain Document

`core/models/chunk.py:Chunk` and `core/models/document.py:Document` are the domain types.
Boundary converters `from_langchain()` / `to_langchain()` exist during migration.

---

## 5. Phase Overview

| Phase | Name                              | Risk     | Key deliverable                                                           |
| ----- | --------------------------------- | -------- | ------------------------------------------------------------------------- |
| 0     | Scaffold & Import Guard           | None     | Directory tree + CI guard                                                 |
| 1     | Generic Registry & Exceptions     | Low      | `Registry[T]`, `OpenRAGError` hierarchy                                   |
| 2     | Domain Models                     | Low      | `Chunk`, `Document`, `User`, `RetrievalQuery`, etc.                       |
| 3     | Configuration Schemas             | Low      | `core/config/` with typed sections + loader                               |
| 4     | ABCs & Ports                      | Low      | 8 co-located ABCs + `__init__.py` re-exports + 13 port ABCs               |
| 5     | Core Domain Logic                 | Medium   | Retrieval, chunking, indexing in `core/`                                  |
| 6     | Inference Adapters                | Medium   | vLLM/Ollama/Infinity clients in `services/inference/`                     |
| 7     | Storage & Persistence             | **High** | God object decomposition -> MilvusStore + PostgresStore(13 repos)         |
| 8     | Orchestrators                     | **High** | 15+ business services in `services/orchestrators/`                        |
| 9     | Workers (Ray)                     | **High** | Thin actor wrappers + pipeline stages                                     |
| 10    | API Layer                         | Medium   | Routers, middleware, schemas in `api/`                                    |
| 11    | Composition Root                  | **High** | `ServiceContainer`, `providers.py`, `factories.py`                        |
| 12    | Internal Cleanup                  | Medium   | Remove shims, delete old `components/`, `routers/`, `models/`             |
| 13    | Project Layout, Infra, Tests & UI | Medium   | Top-level restructure: `infra/`, `scripts/`, `tests/`, `ui/`, Dockerfiles |
| 14    | Per-Partition Presets             | Medium   | DB-backed presets for indexation + retrieval per partition                |
| 15    | OIDC / Keycloak SSO               | Medium   | Dual auth (JWT + API tokens), auto-provisioning, role sync                |

---

## Phase 0 — Scaffold & Import Guard

**Goal:** Create directory skeleton + enforce layer boundaries from commit #1.

### Commits

**0.1 — Create directory tree**

```bash
mkdir -p openrag/core/{config,ports,models,chunking,embeddings,rerankers,llm,vlm,prompts,vector_stores,retrieval,indexing/parsers,observability,utils}
mkdir -p openrag/services/{inference,storage,persistence/migrations/versions,orchestrators,workers/stages,events}
mkdir -p openrag/api/{dependencies,middleware,routers/auth,routers/user,routers/admin,schemas/user,schemas/admin,schemas/auth}
mkdir -p openrag/di
# Touch __init__.py in every dir
```

**0.2 — Add layer import guard**

Create `scripts/check_layer_imports.py`:

```python
"""
CI guard: enforces hexagonal layer dependencies.

Rules:
  core/      -> may NOT import from services/, api/, di/
  services/  -> may NOT import from api/
  api/       -> may NOT import from services/ directly (only via di/)

Usage: python scripts/check_layer_imports.py
Exit code 0 = pass, 1 = violations found.
"""
```

**Verification:** `python -c "import openrag"`, existing tests pass.

---

## Phase 1 — Generic Registry & Exceptions

**Goal:** Build the two foundational utilities that everything else depends on.

### 1.1 — Registry[T]

**File:** `core/utils/registry.py`

The `Registry[T]` API:

```python
class Registry(Generic[T]):
    def __init__(self, kind: str) -> None
    def register(self, name: str) -> Callable[[Type[T]], Type[T]]  # decorator
    def create(self, name: str, **kwargs: Any) -> T
    def get_class(self, name: str) -> Type[T]
    def list_registered(self) -> list[str]
    def __contains__(self, name: str) -> bool
```

Raise `RegistryError` with helpful message listing available implementations.

### 1.2 — Exception hierarchy

**File:** `core/utils/exceptions.py`

Consolidate from `utils/exceptions/{base,vectordb,embeddings}.py`:

```python
class OpenRAGError(Exception):
    """Root. Has code, status_code, to_dict()."""

# Config & registry
class ConfigError(OpenRAGError): ...
class RegistryError(OpenRAGError): ...

# Auth
class AuthError(OpenRAGError): ...
class AuthenticationError(AuthError): ...        # 401

# Validation
class ValidationError(OpenRAGError): ...         # 400/422

# Infrastructure
class ServiceUnavailableError(OpenRAGError): ... # 503
class CircuitBreakerOpenError(ServiceUnavailableError): ...

# Inference
class InferenceError(OpenRAGError): ...          # 503
class LLMParsingError(InferenceError): ...       # 502
class InferenceTimeoutError(InferenceError): ... # 504
class InferenceConnectionError(InferenceError):  # 503

# Storage
class StorageError(OpenRAGError): ...            # 500
class MilvusError(StorageError): ...
class PostgresError(StorageError): ...

# Domain
class NotFoundError(OpenRAGError): ...           # 404
class DocumentNotFoundError(NotFoundError): ...
class PartitionNotFoundError(NotFoundError): ...
class UserNotFoundError(NotFoundError): ...
class QuotaExceededError(OpenRAGError): ...      # 429

# Pipeline
class PipelineError(OpenRAGError): ...
```

### 1.3 — Core utilities

Move pure utility functions (no infra imports):

```
core/utils/text.py              <- components/indexer/utils/text_sanitizer.py
core/utils/dates.py             <- (new)
core/utils/filename.py          <- components/indexer/utils/files.py (filename parts)
core/utils/mime_validation.py   <- routers/utils.py (mime validation logic)
core/utils/logging.py           <- (structlog setup, no Loguru dependency on Ray)
core/utils/retry.py             <- (tenacity config, pure)
core/utils/streaming.py         <- (async token stream helpers)
core/utils/tracing.py           <- (PipelineTrace - per-request timing)
core/utils/debug.py             <- (gated debug file writers)
core/utils/scrub.py             <- (secret scrubbing)
```

### 1.4 — Update old exceptions to re-export

```python
# utils/exceptions/__init__.py
from openrag.core.utils.exceptions import *  # noqa: F401,F403
```

---

## Phase 2 — Domain Models

**Goal:** Create pure Pydantic models in `core/models/`. No infra imports.

### Model inventory

| File                    | Key types                                                                  | Source in OpenRAG                                 |
| ----------------------- | -------------------------------------------------------------------------- | ------------------------------------------------- |
| `chunk.py`              | `Chunk`, `ChunkType`                                                       | Runtime dicts in vectordb                         |
| `document.py`           | `Document`, `ProcessedDocument`, `TextBlock`, `ImageBlock`, `DocumentType` | LangChain `Document`                              |
| `user.py`               | `User`, `PartitionRole`, `UserPartition`                                   | `vectordb/utils.py` User table + `models/user.py` |
| `catalog.py`            | `DocumentRecord`, `IndexationJob`, `DocumentStatus`, `JobStatus`           | `TaskStateManager` + vectordb File table          |
| `query.py`              | `RetrievalQuery`                                                           | `pipeline.py` SearchQueries                       |
| `retrieval_result.py`   | `RetrievalResult`, `ScoredChunk`                                           | Search result dicts                               |
| `retrieval_response.py` | `RetrievalResponse`                                                        | Pipeline return values                            |
| `conversation.py`       | `Conversation`, `Message`                                                  | openai router message handling                    |
| `contextualization.py`  | `ContextualizedQuery`                                                      | pipeline.py query generation                      |
| `prompt.py`             | `Prompt`, `PromptType`                                                     | prompts system                                    |

### Key design decisions

**Chunk model** :

```python
class Chunk(BaseModel):
    id: str                           # UUID
    document_id: str
    text: str
    chunk_index: int = 0
    chunk_type: ChunkType = ChunkType.TEXT
    embedding: list[float] | None = None
    metadata: dict[str, Any] = {}
    partition: str = "default"
    page_number: int | None = None
    token_count: int | None = None
    context: str | None = None        # LLM contextualization text

    def with_embedding(self, embedding: list[float]) -> "Chunk": ...

    # Boundary converters (method body imports only)
    @classmethod
    def from_langchain(cls, doc: Any) -> "Chunk": ...
    def to_langchain(self) -> Any: ...
```

**Document model** :

```python
class Document(BaseModel):
    id: str
    filename: str
    content_type: DocumentType
    text: str | None = None
    raw_bytes: bytes | None = Field(None, exclude=True)
    partition: str = "default"
    tags: list[str] = []
    metadata: dict[str, Any] = {}
```

### Commits

```
2.1  Create core/models/*.py (all domain types)
2.2  Add core/models/__init__.py convenience re-exports
2.3  Add from_langchain/to_langchain converters on Chunk and Document
```

---

## Phase 3 — Configuration Schemas

**Goal:** Split monolithic `config/models.py` into typed sections in `core/config/`.

### File mapping

| New file                        | Source section                                     |
| ------------------------------- | -------------------------------------------------- |
| `core/config/root.py`           | `Settings`                                         |
| `core/config/auth.py`           | env vars (AUTH_TOKEN, SUPER_ADMIN_MODE)            |
| `core/config/chunking.py`       | `ChunkerConfig`                                    |
| `core/config/endpoints.py`      | `ModelEndpointConfig` + `ModelsConfig` (all 4: embedder, reranker, llm, vlm) |
| `core/config/indexation.py`     | `LoaderConfig` + `RayConfig` (indexing parts)      |
| `core/config/infrastructure.py` | `VectorDBConfig` + `RDBConfig`                     |
| `core/config/loader.py`         | `load_config()`                                    |
| `core/config/partition.py`      | (new - per-partition config)                       |
| `core/config/presets.py`        | (new - reusable configs)                           |
| `core/config/retrieval.py`      | `RetrieverConfig` + `RerankerConfig` + `RAGConfig` |

### Key improvement

**ModelEndpointConfig pattern** for inference backends:

```python
class ModelEndpointConfig(BaseModel):
    endpoint: str
    model_name: str | None = None
    batch_size: int = 32
    timeout: float = 30.0
    extra: dict[str, Any] = {}     # implementation-specific kwargs
    context_window: int = 8192

class ModelsConfig(BaseModel):
    embedder: dict[str, ModelEndpointConfig] = {}    # {"default": ..., "fast": ...}
    reranker: dict[str, ModelEndpointConfig] = {}
    llm: dict[str, ModelEndpointConfig] = {}
    vlm: dict[str, ModelEndpointConfig] = {}
```

This enables `make_component_factory()` to look up config by name and create the
right implementation via registry. Future: model endpoints stored in DB, loaded at startup.

### Commits

```
3.1  Create core/config/*.py with typed schemas
3.2  Create core/config/loader.py (YAML + env override logic)
3.3  Update old config/__init__.py to re-export from core/config/
3.4  Verify all existing imports still resolve
```

---

## Phase 4 — ABCs & Ports

**Goal:** Define every contract as an ABC, co-located in its subject folder.
This is the architecturally critical phase.

### 4A — Component ABCs (co-located in subject folders)

Each ABC lives inside its subject folder. The folder's `__init__.py` re-exports
the ABC + registry so consumers get clean imports.

| File                                       | ABC                | Key abstract methods                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------ | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `core/embeddings/embedder.py`              | `Embedder`         | `async embed(texts) -> list[list[float]]`, `async embed_single(text) -> list[float]`, `dimension: int` (property)                                                                                                                                                                                                                                            |
| `core/llm/llm.py`                          | `LLM`              | `async generate(prompt) -> str`, `async chat(messages) -> str`, `async stream_chat(messages) -> AsyncIterator[str]` (default: non-streaming fallback), `async generate_json(prompt) -> dict` (default: parse generate output), `async chat_with_tools(messages, tools) -> dict` (default: NotImplementedError)                                               |
| `core/rerankers/reranker.py`               | `Reranker`         | `async rerank(query, documents, top_k) -> list[tuple[int, float]]`                                                                                                                                                                                                                                                                                           |
| `core/vlm/vlm.py`                          | `VLM`              | `async caption_image(image_bytes, prompt) -> str`, `async caption_images_batch(images, prompt) -> list[str]`                                                                                                                                                                                                                                                 |
| `core/vector_stores/vector_store.py`       | `VectorStore`      | `async upsert(chunks, collection)`, `async search(embedding, top_k, collection, filters)`, `async delete(ids, collection)`, `async ensure_collection(name, dimension)`, `async drop_collection(name)`, `async collection_exists(name)`, `async query_ids_by_filter(collection, filters)`, `async query_chunks_by_filter(collection, filters, output_fields)` |
| `core/ports/catalog_store.py`              | `CatalogStore`     | `async initialize()`, `async shutdown()`, properties: `document_repo`, `job_repo`, `user_repo`, `prompt_repo`, `partition_repo`, ... (13 repos)                                                                                                                                                                                                              |
| `core/chunking/chunking_strategy.py`       | `ChunkingStrategy` | `chunk(document: ProcessedDocument, partition) -> list[Chunk]`                                                                                                                                                                                                                                                                                               |
| `core/indexing/parsers/document_parser.py` | `DocumentParser`   | `async parse(document: Document) -> ProcessedDocument`, `supported_types() -> list[str]`                                                                                                                                                                                                                                                                     |

**Each `__init__.py` re-exports the public surface:**

```python
# core/embeddings/__init__.py
from openrag.core.embeddings.embedder import Embedder
from openrag.core.embeddings.registry import embedder_registry
__all__ = ["Embedder", "embedder_registry"]

# core/rerankers/__init__.py
from openrag.core.rerankers.reranker import Reranker
from openrag.core.rerankers.registry import reranker_registry
__all__ = ["Reranker", "reranker_registry"]

# core/llm/__init__.py
from openrag.core.llm.llm import LLM
from openrag.core.llm.registry import llm_registry
__all__ = ["LLM", "llm_registry"]

# core/vlm/__init__.py
from openrag.core.vlm.vlm import VLM
from openrag.core.vlm.registry import vlm_registry
__all__ = ["VLM", "vlm_registry"]

# core/vector_stores/__init__.py
from openrag.core.vector_stores.vector_store import VectorStore
__all__ = ["VectorStore"]

# core/chunking/__init__.py
from openrag.core.chunking.chunking_strategy import ChunkingStrategy
from openrag.core.chunking.registry import chunking_registry
__all__ = ["ChunkingStrategy", "chunking_registry"]

# core/indexing/parsers/__init__.py
from openrag.core.indexing.parsers.document_parser import DocumentParser
from openrag.core.indexing.parsers.registry import parser_registry
__all__ = ["DocumentParser", "parser_registry"]
```

### 4B — Repository ports (core/ports/)### 2.14 Auth upgrade path

A more complete auth system would use JWT + API keys + bcrypt passwords +
SystemRole (superadmin/admin/user) + PartitionRole (owner/reader). OpenRAG currently
uses SHA-256 token hashing with a simpler role system (viewer/editor/owner).
The refactor should preserve OpenRAG's auth as-is, with the option to upgrade
to JWT/Keycloak later.

Key examples:

```python
# core/ports/document_repo.py
class DocumentRepository(ABC):
    async def create_document(self, doc: DocumentRecord) -> DocumentRecord: ...
    async def get_document(self, document_id: str) -> DocumentRecord | None: ...
    async def list_documents(self, partition: str | list[str] | None = None, ...) -> list[DocumentRecord]: ...
    async def update_document(self, document_id: str, **fields) -> DocumentRecord | None: ...
    async def delete_document(self, document_id: str) -> bool: ...
    async def count_documents(self, partition: str | list[str] | None = None, ...) -> int: ...
    async def get_by_hash_in_partition(self, partition: str, file_hash: str) -> DocumentRecord | None: ...

# core/ports/chunk_repo.py
class ChunkRepository(ABC):
    async def bulk_insert(self, chunks: list[dict]) -> int: ...
    async def get_by_ids(self, chunk_ids: list[str]) -> list[dict]: ...
    async def get_by_document_id(self, document_id: str) -> list[dict]: ...
    async def delete_by_document_id(self, document_id: str) -> int: ...
    async def bm25_search(self, query_text: str, partition: str, top_k: int = 20) -> list[dict]: ...

# core/ports/user_repo.py
class UserRepository(ABC):
    async def create_user(self, user: User) -> User: ...
    async def get_user(self, user_id: str) -> User | None: ...
    async def get_user_by_token(self, token_hash: str) -> User | None: ...
    async def list_users(self, ...) -> list[User]: ...
    # + partition assignment methods
```

### Commits

```
4.1  Create core/embeddings/{embedder.py, registry.py, __init__.py}
4.2  Create core/rerankers/{reranker.py, registry.py, __init__.py}
4.3  Create core/llm/{llm.py, registry.py, __init__.py}
4.4  Create core/vlm/{vlm.py, registry.py, __init__.py}
4.5  Create core/vector_stores/{vector_store.py, __init__.py}
4.6  Create core/chunking/{chunking_strategy.py, registry.py, __init__.py}
4.7  Create core/indexing/parsers/{document_parser.py, registry.py, __init__.py}
4.8  Create all port ABCs in core/ports/ (including catalog_store.py)
```

---

## Phase 5 — Core Domain Logic

**Goal:** Move pure business logic into `core/`. First phase that MOVES code.

### 5A — Retrieval core

| Target                        | Source                                                | Key change                                      |
| ----------------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| `core/retrieval/rrf.py`       | `components/reranker/base.py` rrf_reranking()         | Pure math, no changes                           |
| `core/retrieval/retriever.py` | `components/retriever.py` ABCRetriever hierarchy      | Uses VectorStore port instead of get_vectordb() |
| `core/retrieval/hydration.py` | `components/retriever.py` _expand_with_related_chunks | Uses VectorStore port                           |
| `core/retrieval/pipeline.py`  | `components/pipeline.py` RetrieverPipeline            | Orchestrates via injected ports                 |

**Critical:** Retriever strategies call `VectorStore.search()` (port method), not
`vectordb.async_search.remote()` (Ray call). The Ray call moves to the adapter.

### 5B — Chunking strategies

| Target                              | Source                                                    |
| ----------------------------------- | --------------------------------------------------------- |
| `core/chunking/recursive.py`        | `components/indexer/chunker/chunker.py` RecursiveSplitter |
| `core/chunking/markdown_section.py` | `components/indexer/chunker/utils.py` markdown parsing    |

Register via decorator: `@chunking_registry.register("recursive")`.

### 5C — Prompt builders

| Target                                      | Source                                                            |
| ------------------------------------------- | ----------------------------------------------------------------- |
| `core/prompts/chat_prompt_builder.py`       | `components/pipeline.py` context formatting + system prompt       |
| `core/prompts/vlm_prompt_builder.py`        | `components/indexer/loaders/base.py` VLM prompt logic             |
| `core/prompts/contextualization_builder.py` | `components/indexer/chunker/chunker.py` contextualization prompts |
| `core/prompts/query_rewriter.py`            | `components/retriever.py` multi-query + HyDE prompt building      |
| `core/prompts/map_reduce_builder.py`        | `components/map_reduce.py` map/reduce prompts                     |
| `core/prompts/template_loader.py`           | `components/prompts/prompts.py` disk-based template loading       |

### 5D — Indexing domain logic

| Target                               | Source                                                      |
| ------------------------------------ | ----------------------------------------------------------- |
| `core/indexing/contextualize.py`     | `components/indexer/chunker/chunker.py` ChunkContextualizer |
| `core/indexing/text_preprocessor.py` | `components/indexer/utils/text_sanitizer.py`                |
| `core/indexing/validators.py`        | `routers/utils.py` validation functions                     |
| `core/indexing/parsers/*.py`         | `components/indexer/loaders/*.py`                           |

### Commits

```
5.1-5.4   Retrieval core (rrf, retriever, hydration, pipeline)
5.5-5.6   Chunking strategies
5.7-5.12  Prompt builders (chat, vlm, contextualization, query rewriter, map-reduce, template loader)
5.13-5.17 Indexing domain logic + parsers
5.15      Update old files to re-export from core/
```

---

## Phase 6 — Inference Adapters

**Goal:** Move all HTTP client code to `services/inference/`. Register with core registries.

### Files

| Target                                        | Source                                                          | Registers as                                                            |
| --------------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `services/inference/vllm_client.py`           | `components/llm.py` + `components/indexer/embeddings/openai.py` | `@llm_registry.register("vllm")`, `@embedder_registry.register("vllm")` |
| `services/inference/vlm_client.py`            | `components/indexer/loaders/base.py` VLM methods                | `@vlm_registry.register("vllm")`                                        |
| `services/inference/infinity_client.py`       | `components/reranker/infinity.py`                               | `@reranker_registry.register("infinity")`                               |
| `services/inference/distributed_semaphore.py` | `components/utils.py` DistributedSemaphore*                     | Ray-based cluster-wide limiter                                          |
| `services/inference/_circuit_breaker.py`      | (new)                                                           | `@with_circuit_breaker` decorator (aiobreaker)                          |
| `services/inference/_retry.py`                | (new)                                                           | `@with_retry` decorator (tenacity + jitter)                             |
| `services/inference/_timeout.py`              | (new)                                                           | `@with_timeout` decorator (asyncio.timeout)                             |
| `services/inference/healthcheck.py`           | `routers/utils.py` get_openai_models()                          | Endpoint readiness probes                                               |

### Key pattern

**vLLM client with distributed semaphore:**

```python
@llm_registry.register("vllm")
class VLLMClient(LLM):
    def __init__(self, endpoint: str, model_name: str | None = None, timeout: float = 120.0, ...):
        self._client = httpx.AsyncClient(...)
        self._semaphore = DistributedLLMSemaphore(...)

    async def chat(self, messages, **kwargs):
        async with self._slot():   # acquire distributed semaphore
            response = await self._client.post(f"{self._endpoint}/v1/chat/completions", ...)
            return response.json()["choices"][0]["message"]["content"]
```

### Commits

```
6.1  Create services/inference/vllm_client.py (LLM + Embedder)
6.2  Create services/inference/infinity_client.py (Reranker)
6.3  Create services/inference/vlm_client.py (VLM)
6.4  Create services/inference/distributed_semaphore.py
6.5  Create services/inference/_circuit_breaker.py + _retry.py + _timeout.py
       (shared resilience decorators applied by all inference clients)
6.6  Create services/inference/healthcheck.py
6.7  Update old files to re-export
```

---

## Phase 7 — Storage & Persistence Adapters

**This is the highest-risk phase.** The current `Vectordb` Ray actor (god object) must be
decomposed into:

- `MilvusVectorStore` (implements `VectorStore`)
- `PostgresStore` (implements `CatalogStore`, owns 13 repos)

### 7A — PostgreSQL persistence layer

**Follow the composite pattern:**

**ConnectionManager** (`services/persistence/connection.py`):

```python
class ConnectionManager:
    def __init__(self, config: PostgresConfig):
        self._dsn = build_dsn(config)
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, ...)

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Not initialized")
        return self._pool

    async def shutdown(self) -> None:
        if self._pool:
            await self._pool.close()
```

**Repository implementations** — extract from `PartitionFileManager`:

| New file                        | Source methods                                                                 | Implements            |
| ------------------------------- | ------------------------------------------------------------------------------ | --------------------- |
| `persistence/document_repo.py`  | add_file, remove_file, update_file, list_files, file_exists                    | `DocumentRepository`  |
| `persistence/user_repo.py`      | create_user, get_user, get_user_by_token, delete_user, update_user, list_users | `UserRepository`      |
| `persistence/partition_repo.py` | create_partition, delete_partition, list_partitions, partition_exists          | `PartitionRepository` |
| `persistence/job_repo.py`       | TaskStateManager state methods                                                 | `JobRepository`       |
| `persistence/chunk_repo.py`     | (new - for future BM25 in PG)                                                  | `ChunkRepository`     |
| ... and 8 more repos            |                                                                                |                       |

**PostgresStore** (`services/storage/postgres_store.py`):

```python
class PostgresStore(CatalogStore):
    def __init__(self, config: PostgresConfig):
        self._conn = ConnectionManager(config)
        pool_getter = lambda: self._conn.pool
        self._documents = PgDocumentRepository(pool_getter)
        self._users = PgUserRepository(pool_getter)
        self._partitions = PgPartitionRepository(pool_getter)
        self._jobs = PgJobRepository(pool_getter)
        # ... 13 total

    @property
    def document_repo(self) -> DocumentRepository:
        return self._documents
    # ... etc
```

### 7B — Milvus vector store

**MilvusVectorStore** (`services/storage/milvus_store.py`):

```python
class MilvusVectorStore(VectorStore):
    def __init__(self, config: VectorDBConfig):
        uri = f"http://{config.host}:{config.port}"
        self._client = MilvusClient(uri=uri)
        self._async_client = AsyncMilvusClient(uri=uri)
        self._hybrid = config.hybrid_search

    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int: ...
    async def search(self, embedding, top_k, collection, filters) -> list[dict]: ...
    async def delete(self, ids, collection) -> int: ...
```

OpenRAG uses Milvus BM25 (sparse vectors),
The alternative (Postgres tsvector) is not used here. Keep OpenRAG's hybrid search approach.

**Embedding removed from vector store.** Currently `MilvusDB.async_add_documents()`
embeds chunks internally. After refactor, embedding happens in the pipeline stage
BEFORE calling `vector_store.upsert()`. The vector store receives pre-embedded chunks.

### 7C — Shim the god object

After creating the new stores, update `vectordb.py` to delegate:

```python
# Old MilvusDB actor becomes a thin wrapper
class MilvusDB:
    def __init__(self):
        config = load_config()
        self._vector_store = MilvusVectorStore(config.vectordb)
        self._catalog_store = PostgresStore(config.rdb)
        # ... delegate all methods
```

### Commits

```
7.1   services/persistence/connection.py
7.2   services/persistence/schema.py (copy SQLAlchemy models)
7.3   services/persistence/document_repo.py
7.4   services/persistence/user_repo.py
7.5   services/persistence/partition_repo.py
7.6   services/persistence/job_repo.py
7.7   services/persistence/chunk_repo.py + remaining repos
7.8   services/storage/milvus_store.py
7.9   services/storage/postgres_store.py (composite)
7.10  Update MilvusDB actor to delegate to new stores (shim)
7.11  Integration test: full upload-search cycle works
```

---

## Phase 8 — Orchestrators

**Goal:** Business services in `services/orchestrators/` that coordinate ports.

### Key pattern

Orchestrators receive **factory callables**, not instances:

```python
class RetrievalService:
    def __init__(
        self,
        vector_store: VectorStore,
        embedder_factory: Callable[[str], Embedder],
        reranker_factory: Callable[[str], Reranker],
        llm_factory: Callable[[str], LLM],
        config: Settings,
        chunk_repo: ChunkRepository | None = None,
    ):
        self._vector_store = vector_store
        self._embedder_factory = embedder_factory
        # ...

    async def retrieve(self, query: str, partition: str, **kwargs):
        embedder = self._embedder_factory("default")
        embedding = await embedder.embed_single(query)
        results = await self._vector_store.search(embedding, ...)
        if self._reranker_factory:
            reranker = self._reranker_factory("default")
            results = await reranker.rerank(query, results, ...)
        return results
```

### Orchestrator inventory

| Service                   | Source                                       | Dependencies (injected)                                   |
| ------------------------- | -------------------------------------------- | --------------------------------------------------------- |
| `auth_service.py`         | `routers/utils.py` + `api.py` AuthMiddleware | UserRepository, AuthConfig                                |
| `user_service.py`         | `routers/users.py`                           | UserRepository                                            |
| `partition_service.py`    | `routers/partition.py`                       | PartitionRepository, VectorStore                          |
| `document_service.py`     | vectordb file ops                            | DocumentRepository, VectorStore                           |
| `retrieval_service.py`    | `pipeline.py` RetrieverPipeline              | VectorStore, EmbedderFactory, RerankerFactory, LLMFactory |
| `query_service.py`        | `pipeline.py` RagPipeline                    | RetrievalService, LLMFactory, PromptService               |
| `query_orchestrator.py`   | `pipeline.py` generate_query                 | LLMFactory, config                                        |
| `indexing_service.py`     | `indexer.py` add_file flow                   | VectorStore, CatalogStore, config                         |
| `job_service.py`          | TaskStateManager                             | JobRepository                                             |
| `conversation_service.py` | openai router chat logic                     | ConversationRepository, LLMFactory                        |
| `prompt_service.py`       | `prompts/prompts.py`                         | PromptRepository                                          |
| `conversion_service.py`   | `loaders/serializer.py`                      | ParserRegistry, VLMFactory                                |
| `cluster_service.py`      | `routers/actors.py`                          | Ray introspection                                         |
| `llm_contextualizer.py`   | `pipeline.py` query gen                      | LLMFactory                                                |
| `research_planner.py`     | multi-query logic                            | LLMFactory                                                |

### Commits

```
8.1-8.15  One commit per orchestrator (create + wire)
```

---

## Phase 9 — Workers (Ray Isolation)

**Goal:** All Ray code in `services/workers/`. Core and orchestrators are Ray-free.

### IndexerActor — thin wrapper pattern

```python
# services/workers/indexer_actor.py
@ray.remote(max_concurrency=2, max_restarts=3)
class IndexerActorClass:
    def __init__(self):
        # Side-effect imports to register with registries
        import openrag.services.inference.vllm_client  # noqa
        import openrag.services.inference.infinity_client  # noqa

    async def process_document(self, doc_dict: dict, config_dict: dict, ...) -> dict:
        document = Document.model_validate(doc_dict)
        config = Settings.model_validate(config_dict)
        result = await ray_data_ingest_documents([document], config, ...)
        return {"status": "success", **result}
```

### Pipeline stages

```
services/workers/stages/parse.py            <- loaders/serializer.py
services/workers/stages/caption.py          <- loaders/base.py VLM calls
services/workers/stages/chunk.py            <- chunker flow
services/workers/stages/contextualize.py    <- ChunkContextualizer
services/workers/stages/embed.py            <- embedding flow
services/workers/stages/store.py            <- vectordb insert flow
```

Each stage follows the same pattern: async function, timeout with base + per-chunk
scaling, in-place row mutation, error marking, credential scrubbing after use.

### Commits

```
9.1  services/workers/ray_utils.py
9.2  services/workers/stages/*.py
9.3  services/workers/pipeline_builder.py
9.4  services/workers/indexer_actor.py (thin wrapper)
9.5  Integration test: indexing pipeline e2e
```

---

## Phase 10 — API Layer Restructure

**Goal:** Move all FastAPI code to `api/` with clean DI.

### 10A — Error handlers

 `api/error_handlers.py` maps domain exceptions to HTTP via MRO walk:

```python
_STATUS_MAP = {
    NotFoundError: 404,
    AuthenticationError: 401,
    AuthError: 403,
    ValidationError: 422,
    InferenceTimeoutError: 504,
    LLMParsingError: 502,
    InferenceError: 503,
    ServiceUnavailableError: 503,
    StorageError: 500,
    QuotaExceededError: 429,
}
```

Response includes `request_id` from structlog contextvars.

### 10B — Middleware

| Target                               | Source                                       |
| ------------------------------------ | -------------------------------------------- |
| `api/middleware/request_id.py`       | (new)                                        |
| `api/middleware/instrumentation.py`  | `routers/monitoring.py` MonitoringMiddleware |
| `api/middleware/security_headers.py` | (new)                                        |
| `api/middleware/request_timeout.py`  | (new)                                        |
| `api/middleware/idempotency.py`      | (new)                                        |

### 10C — Auth dependencies

```python
# api/dependencies/auth.py
async def get_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    token = _extract_token(request)
    if not token:
        raise AuthenticationError("Missing authentication")
    return await auth_service.authenticate(token)
```

### 10D — Routers

All routers use `Depends(get_service)` from `di/providers.py`:

```python
@router.get("/search")
async def search(
    service: RetrievalService = Depends(get_retrieval_service),
    query: str = Query(...),
    partition: str = Query("default"),
):
    return await service.retrieve(query, partition)
```

### 10E — main.py

```python
# api/main.py
from openrag.di.container import ServiceContainer
from openrag.di.providers import set_container

async def lifespan(app: FastAPI):
    container = ServiceContainer()
    set_container(container)
    await container.initialize()
    yield
    await container.shutdown()

app = FastAPI(lifespan=lifespan)
# register middleware, routers, error handlers
```

### Commits

```
10.1   api/error_handlers.py
10.2   api/middleware/*.py
10.3   api/dependencies/auth.py
10.4   api/schemas/**/*.py
10.5   api/routers/user/health.py (simplest, proof of concept)
10.6-10.15  Remaining routers (one commit each)
10.16  api/main.py (wire everything, keep old routers in parallel)
10.17  Remove old routers from mounts (one at a time)
```

---

## Phase 11 — Composition Root (DI)

**Goal:** `ServiceContainer` wires everything. Remove all global singletons.

### container.py — sync/async lifecycle

```python
class ServiceContainer:
    def __init__(self, config: Settings | None = None):
        # 1. Load config
        self._config = config or load_config()
        setup_logging(self._config)

        # 2. Register implementations (side-effect imports)
        register_embedders()    # di/embedders.py
        register_rerankers()
        register_llms()
        register_vlms()

        # 3. Create infrastructure stores
        self._postgres_store = PostgresStore(self._config.infrastructure.rdb)
        self._milvus_store = MilvusVectorStore(self._config.infrastructure.vectordb)

        # 4. Create component factories (thread-safe, cached)
        self._client_caches: list[dict] = []
        self._embedder_factory = make_component_factory(
            embedder_registry, self._config.models.embedder, "vllm", self._client_caches
        )
        self._reranker_factory = make_component_factory(
            reranker_registry, self._config.models.reranker, "infinity", self._client_caches
        )
        self._llm_factory = make_component_factory(
            llm_registry, self._config.models.llm, "vllm", self._client_caches
        )
        self._vlm_factory = make_component_factory(
            vlm_registry, self._config.models.vlm, "vllm", self._client_caches
        )

        # 5. Create orchestrator services
        self._auth_service = AuthService(self._config.auth, self._postgres_store.user_repo)
        self._retrieval_service = RetrievalService(
            self._milvus_store, self._embedder_factory, self._reranker_factory,
            self._llm_factory, self._config,
        )
        self._indexing_service = IndexingService(
            self._milvus_store, self._config,
        )
        # ... all other services

    async def initialize(self):
        """Called from FastAPI lifespan. Does all async I/O."""
        await self._postgres_store.initialize()
        self._postgres_store.run_migrations()
        await self._milvus_store.ensure_collection(...)
        # seed defaults...
        self._initialized = True

    async def shutdown(self):
        for cache in self._client_caches:
            for client in cache.values():
                if hasattr(client, "aclose"):
                    await client.aclose()
        await self._postgres_store.shutdown()
```

### providers.py

```python
_container: ServiceContainer | None = None
_lock = threading.Lock()

def set_container(c: ServiceContainer):
    global _container
    with _lock:
        _container = c

def _require_initialized() -> ServiceContainer:
    if _container is None or not _container.is_initialized:
        raise RuntimeError("Container not initialized")
    return _container

def get_retrieval_service() -> RetrievalService:
    return _require_initialized().retrieval_service

def get_config() -> Settings:
    return _require_initialized().config
# ... one getter per service
```

### factories.py — make_component_factory()

The make_component_factory() implementation:

```python
def make_component_factory(
    registry: Registry[T],
    config_section: dict[str, ModelEndpointConfig],
    default_impl: str,
    client_caches: list[dict[str, T]],
    extra_kwargs_fn: Callable | None = None,
) -> Callable[[str], T]:
    cache: dict[str, T] = {}
    lock = threading.Lock()
    client_caches.append(cache)

    def factory(name: str = "default") -> T:
        if name in cache:
            return cache[name]
        with lock:
            if name in cache:
                return cache[name]
            model_cfg = config_section[name]
            impl = model_cfg.extra.get("implementation", default_impl)
            kwargs = {"endpoint": model_cfg.endpoint, "model_name": model_cfg.model_name, ...}
            if extra_kwargs_fn:
                kwargs.update(extra_kwargs_fn(model_cfg))
            instance = registry.create(impl, **kwargs)
            cache[name] = instance
            return instance

    return factory
```

### Registration modules

```python
# di/embedders.py
def register_embedders() -> None:
    import openrag.services.inference.vllm_client  # noqa: F401

# di/rerankers.py
def register_rerankers() -> None:
    import openrag.services.inference.infinity_client  # noqa: F401

# di/llms.py
def register_llms() -> None:
    import openrag.services.inference.vllm_client  # noqa: F401

# di/vlms.py
def register_vlms() -> None:
    import openrag.services.inference.vlm_client  # noqa: F401
```

### Commits

```
11.1  di/factories.py (make_component_factory)
11.2  di/embedders.py, di/rerankers.py, di/llms.py, di/vlms.py
11.3  di/repositories.py, di/vector_stores.py
11.4  di/container.py (ServiceContainer)
11.5  di/providers.py
11.6  Wire into api/main.py lifespan
11.7  Update routers to use Depends() from providers (one at a time)
11.8  Remove global singletons from utils/dependencies.py
11.9  Remove module-level config = load_config() calls
```

---

## Phase 12 — Internal Cleanup & Remove Shims

**Goal:** Delete all backward-compatibility re-exports and old internal code inside
the `openrag/` Python package. After this phase, only the new 3-layer structure
remains inside the package.

### Commit sequence

```
12.1   Run import guard - verify zero violations
12.2   Remove components/retriever.py (-> core/retrieval/)
12.3   Remove components/reranker/ (-> core/rerankers/ + services/inference/)
12.4   Remove components/llm.py (-> core/llm/ + services/inference/)
12.5   Remove components/pipeline.py (-> services/orchestrators/)
12.6   Remove components/map_reduce.py (-> core/prompts/map_reduce_builder.py)
12.7   Remove components/utils.py (-> core/utils/ + services/inference/)
12.8   Remove components/indexer/ (-> core/indexing/ + services/)
12.9   Remove components/websearch/ (-> services/ or keep as adapter)
12.10  Remove routers/ (-> api/routers/)
12.11  Remove models/ (-> core/models/ + api/schemas/)
12.12  Remove utils/dependencies.py (-> di/)
12.13  Remove utils/exceptions/ (-> core/utils/exceptions.py)
12.14  Remove config/ (-> core/config/)
12.15  Delete empty components/, routers/, models/, utils/ directories
12.16  Verify: python -c "import openrag" + all tests pass
```

---

## Phase 13 — Project Layout, Infra, Tests & UI

**Goal:** Restructure the top-level project layout from the current flat/scattered
structure to the clean target layout. Move deployment, tests, scripts, and UI
to their proper locations.

### Current top-level layout (what exists today)

```
openrag_1.1.7/
|-- openrag/                     # Python package (mixed: code + scripts + tests + static)
|   |-- scripts/                 # CLI tools + Alembic migrations (INSIDE package)
|   |-- tests/                   # Some unit tests (INSIDE package)
|   |-- public/                  # Static assets (INSIDE package)
|   +-- app_front.py             # Chainlit frontend (INSIDE package)
|
|-- Dockerfile                   # Root-level (no infra/ folder)
|-- Dockerfile.ray               # Root-level
|-- docker-compose.yaml          # Root-level
|-- entrypoint.sh                # Root-level
|-- conf/                        # Config YAML (OK, stays)
|-- tests/                       # Integration tests + Robot Framework
|   |-- api_tests/               # pytest integration
|   +-- api/                     # Robot Framework
|-- extern/                      # Submodules: vllm, reranker, indexer-ui
|-- prompts/                     # Prompt templates (OK, stays or moves into openrag/)
|-- openrag_metrics/             # Grafana + Prometheus configs
|-- ansible/                     # Ansible deployment
|-- charts/                      # Helm charts
|-- vdb/                         # Milvus config
|-- benchmarks/                  # Performance tests
|-- quick_start/                 # Getting started examples
|-- utility/                     # Misc utility scripts
|-- docs/                        # Documentation site (Astro)
+-- pyproject.toml, uv.lock, pytest.ini, etc.
```

### Target top-level layout

```
openrag/
|-- openrag/                     # Python package (ONLY application code)
|   |-- core/
|   |-- services/
|   |-- api/
|   |-- di/
|   +-- prompts/                 # Prompt templates (disk-loaded by the app)
|
|-- conf/                        # YAML configuration files per environment
|-- infra/                       # ALL deployment infrastructure
|   |-- docker/
|   |   |-- api.Dockerfile       # <- was Dockerfile
|   |   +-- ray.Dockerfile       # <- was Dockerfile.ray
|   |-- compose/
|   |   |-- docker-compose.yaml  # <- was root docker-compose.yaml
|   |   |-- .env.example
|   |   |-- grafana/             # <- was openrag_metrics/grafana
|   |   |-- prometheus/          # <- was openrag_metrics/prometheus
|   |   |-- milvus/              # <- was root vdb/
|   |   |-- nginx/               # reverse proxy config
|   |   +-- postgres/            # Postgres init scripts if any
|   |-- scripts/
|   |   +-- entrypoint.sh        # <- was root entrypoint.sh
|   |-- ansible/                 # <- was root ansible/
|   +-- charts/                  # <- was root charts/
|
|-- scripts/                     # Operational CLI tools
|   |-- migrate.py               # <- was openrag/scripts/migrations/
|   |-- backup.py                # <- was openrag/scripts/backup.py
|   |-- restore.py               # <- was openrag/scripts/restore.py
|   |-- embed.py                 # <- was openrag/scripts/embed.py
|   +-- check_file_counts.py     # <- was openrag/scripts/check_file_counts.py
|
|-- tests/                       # ALL tests (unified)
|   |-- unit/                    # Unit tests (mirrors openrag/ package structure)
|   |   |-- core/
|   |   |   |-- test_registry.py
|   |   |   |-- test_rrf.py
|   |   |   +-- test_chunk_model.py
|   |   |-- services/
|   |   |   |-- test_milvus_store.py
|   |   |   +-- test_vllm_client.py
|   |   +-- api/
|   |       +-- test_error_handlers.py
|   |-- integration/             # End-to-end tests (need running services)
|   |   |-- test_indexer.py      # <- was tests/api_tests/test_indexer.py
|   |   |-- test_search.py       # <- was tests/api_tests/test_search.py
|   |   |-- test_openai_compat.py
|   |   |-- test_users.py
|   |   |-- test_partitions.py
|   |   |-- test_workspaces.py
|   |   |-- conftest.py          # <- was tests/api_tests/conftest.py
|   |   +-- mock_vllm.py         # <- was tests/api_tests/api_run/mock_vllm.py
|   |-- load/                    # Performance/load tests
|   |   +-- (from benchmarks/)
|   +-- conftest.py              # Root conftest with markers: unit, integration, slow
|
|-- docs/                        # Human-readable documentation
|-- ui/                          # Admin frontend (indexer-ui submodule or standalone)
|-- pyproject.toml
+-- uv.lock
```

### 13A — Move deployment infrastructure to infra/

```
13A.1  Create infra/{docker,compose,scripts} directories
13A.2  Move Dockerfile -> infra/docker/api.Dockerfile
         Update build context and paths inside the Dockerfile
13A.3  Move Dockerfile.ray -> infra/docker/ray.Dockerfile
13A.4  Move docker-compose.yaml -> infra/compose/docker-compose.yaml
         Update build.context, build.dockerfile paths
         Update volume mount paths
13A.5  Move entrypoint.sh -> infra/scripts/entrypoint.sh
         Update Dockerfile COPY to match new path
13A.6  Move service configs into infra/compose/ (alongside docker-compose):
         openrag_metrics/grafana -> infra/compose/grafana/
         openrag_metrics/prometheus -> infra/compose/prometheus/
         vdb/ -> infra/compose/milvus/
         (add nginx/, postgres/ subdirs as needed)
13A.7  Move ansible/ -> infra/ansible/
13A.8  Move charts/ -> infra/charts/
13A.9  Verify: docker compose -f infra/compose/docker-compose.yaml build
```

### 13B — Move scripts out of the Python package

Currently `openrag/scripts/` lives inside the Python package, which means CLI tools
are bundled in the Docker image and can import from `openrag.*` but are mixed with
application code.

```
13B.1  Create top-level scripts/ directory
13B.2  Move openrag/scripts/backup.py -> scripts/backup.py
13B.3  Move openrag/scripts/restore.py -> scripts/restore.py
13B.4  Move openrag/scripts/embed.py -> scripts/embed.py
13B.5  Move openrag/scripts/check_file_counts.py -> scripts/check_file_counts.py
13B.6  Move openrag/scripts/filter-logs.py -> scripts/filter_logs.py
13B.7  Create scripts/migrate.py wrapping Alembic
         Migrations stay in services/persistence/migrations/ (part of the package)
         The CLI script just calls alembic programmatically
13B.8  Move shell scripts (backup.sh.example, etc.) -> scripts/
13B.9  Remove empty openrag/scripts/ directory
13B.10 Update any Docker CMD or documentation referencing old script paths
```

### 13C — Restructure tests

Currently tests are split between `openrag/**/test_*.py` (inside the package) and
`tests/api_tests/` (outside). Unify into a single `tests/` tree.

```
13C.1  Create tests/{unit,integration,load} directories
13C.2  Create tests/conftest.py with shared fixtures and markers:
         @pytest.mark.unit, @pytest.mark.integration, @pytest.mark.slow
13C.3  Move tests/api_tests/*.py -> tests/integration/
         Update imports in test files
13C.4  Move tests/api_tests/conftest.py -> tests/integration/conftest.py
13C.5  Move tests/api_tests/api_run/mock_vllm.py -> tests/integration/mock_vllm.py
13C.6  Move inline test files from openrag/ to tests/unit/:
         openrag/components/indexer/chunker/test_chunking.py -> tests/unit/core/test_chunking.py
         openrag/components/reranker/test_rrf_reranking.py -> tests/unit/core/test_rrf.py
         openrag/test_token_validation.py -> tests/unit/test_token_validation.py
         openrag/test_version.py -> tests/unit/test_version.py
         ... (all test_*.py files inside openrag/)
13C.7  Move benchmarks/ -> tests/load/
13C.8  Update pytest.ini / pyproject.toml [tool.pytest]:
         testpaths = ["tests"]
         markers:
           unit: Unit tests (no external services)
           integration: Integration tests (need running services)
           slow: Long-running tests
13C.9  Add CI-friendly test commands:
         uv run pytest -m unit                    # fast, no infra needed
         uv run pytest -m integration             # needs docker services
         uv run pytest -m "not slow"              # skip load tests
13C.10 Verify: uv run pytest -m unit passes
13C.11 Remove tests/api/ Robot Framework tests or move to tests/robot/
13C.12 Remove old tests/ subdirectories
```

### 13D — Prompts location

Move prompts inside the Python package so they're bundled with the app:

```
13D.1  Move prompts/ -> openrag/prompts/
         (or keep at root and update config paths — choose one)
13D.2  Update core/config paths to reference new prompts location
13D.3  Update Dockerfile COPY to include openrag/prompts/
```

### 13E — UI submodule

The admin frontend currently lives in `extern/indexer-ui` as a git submodule.

```
13E.1  Move extern/indexer-ui -> ui/
         Or: keep as submodule but reference from ui/ symlink
13E.2  Update docker-compose service for indexer-ui to use new path
13E.3  Remove extern/ directory (or keep for vllm/reranker submodules if still needed)
```

### 13F — pyproject.toml and root cleanup

```
13F.1  Update pyproject.toml:
         - Update package name if renaming
         - Update [tool.pytest] testpaths
         - Update [tool.ruff] src paths
         - Verify [project.scripts] entry points
13F.2  Update .github/workflows/ CI:
         - Test commands use new paths
         - Docker build context uses infra/docker/
         - docker-compose -f infra/compose/docker-compose.yaml
13F.3  Remove root-level files that moved:
         - Dockerfile, Dockerfile.ray (now in infra/docker/)
         - docker-compose.yaml (now in infra/compose/)
         - entrypoint.sh (now in infra/scripts/)
         - pytest.ini (config now in pyproject.toml)
13F.4  Remove stale directories:
         - quick_start/ (move useful content to docs/ or remove)
         - utility/ (merge into scripts/ or remove)
         - openrag.egg-info/ (regenerated on build)
         - model_weights/, logs/ (runtime dirs, add to .gitignore)
13F.5  Update README.md with new project layout
13F.6  Update CLAUDE.md with new paths and commands
13F.7  Final verification:
         - uv sync
         - uv run pytest -m unit
         - docker compose -f infra/compose/docker-compose.yaml build
         - docker compose -f infra/compose/docker-compose.yaml up -d
         - Integration tests pass
```

---

## Phase 14 — Per-Partition Presets (Indexation & Retrieval)

**Goal:** Add the presetting mechanism that gives fine-grained
indexation and retrieval configuration per partition. This is a feature addition
on top of the clean architecture, not a refactoring step.

**Key files to create:**

- `core/config/partition.py` — `PartitionConfig`, `PartitionRow`
- `core/config/presets.py` — `PresetsConfig`, `PresetRow`
- `core/config/indexation.py` — `IndexationPipelineConfig` (25+ fields)
- `core/config/retrieval.py` — `RetrievalPipelineConfig`, `IntentStrategyConfig`
- `services/orchestrators/partition_service.py` — loads partitions into config
- `services/orchestrators/preset_service.py` — preset CRUD + seeding
- `services/persistence/partition_repo.py` — `PartitionRow` persistence
- `services/persistence/preset_repo.py` — `PresetRow` persistence
- `api/routers/admin/presets.py` — preset admin endpoints
- `api/routers/admin/pipelines.py` — dry-run preview endpoint

### 14A — Config models (already scaffolded in Phase 3)

Flesh out the config models created in Phase 3 with full pipeline detail:

**`core/config/indexation.py`** — `IndexationPipelineConfig`:

```python
class IndexationPipelineConfig(BaseModel):
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    parsing_strategy: str = "marker"
    vlm: str | None = None
    enable_image_captioning: bool = True
    enable_contextualization: bool = False
    contextualization_llm: str | None = None
    contextualization_mode: str = "structured"    # none | simple | structured
    contextualization_window: int = 1
    contextualization_max_tokens: int = 2048
    enable_metadata_extraction: bool = True
    metadata_extraction_llm: str | None = None
    enable_entity_extraction: bool = True
    entity_labels: list[str] = ["person", "organization", "location", "event"]
    enable_topic_tagging: bool = True
    max_topic_tags: int = 7
    topic_tagging_llm: str | None = None
    # prompt override names (resolved via PromptService)
    contextualization_prompt_name: str | None = None
    vlm_caption_prompt_name: str | None = None
    image_caption_prompt_name: str | None = None
```

**`core/config/retrieval.py`** — `RetrievalPipelineConfig`:

```python
class IntentStrategyConfig(BaseModel):
    pipeline: str = "unified"
    top_k: int = 10
    top_n: int = 5

class RetrievalPipelineConfig(BaseModel):
    type: str = "unified"
    reranker: str | None = None
    llm: str | None = None
    top_k: int = 20
    top_n: int = 10
    enable_reranker: bool = True
    enable_planner: bool = True
    intent_strategies: dict[str, IntentStrategyConfig] = Field(default_factory=_default_intent_strategies)
    rrf_k: int = 60
```

**`core/config/partition.py`** — `PartitionConfig`:

```python
class PartitionConfig(BaseModel):
    name: str
    description: str = ""
    embedder: str = "default"
    indexation: IndexationPipelineConfig = Field(default_factory=IndexationPipelineConfig)
    retrieval: RetrievalPipelineConfig = Field(default_factory=RetrievalPipelineConfig)
    collection_name: str | None = None
    chat_history_depth: int = 0
    chat_llm: str | None = None

class PartitionRow(BaseModel):
    """DB representation — references presets by name."""
    name: str
    display_name: str | None = None
    description: str = ""
    embedder: str = "default"
    indexation_preset: str = "default"
    retrieval_preset: str = "default"
    dimension: int = 1024
    collection_name: str | None = None
    chat_history_depth: int = 0
    chat_llm: str | None = None
```

**`core/config/presets.py`** — `PresetRow`:

```python
class PresetRow(BaseModel):
    name: str
    preset_type: str          # "indexation" | "retrieval"
    config: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime
```

### 14B — Database layer

**New port ABCs** (already created as stubs in Phase 4):

- `core/ports/preset_repo.py` — `PresetRepository`: `get(name, type)`, `list(type)`, `upsert(preset)`, `delete(name, type)`
- `core/ports/partition_repo.py` — extend with `get_partition_config(name)`, `update_partition_config(name, **fields)`

**New persistence implementations:**

- `services/persistence/preset_repo.py` — PostgreSQL CRUD for `PresetRow`
- `services/persistence/partition_repo.py` — extend with preset-reference columns

**Alembic migration:**

```
services/persistence/migrations/versions/NNN_add_presets_and_partition_config.py
  - CREATE TABLE presets (name, preset_type, config JSONB, created_at, updated_at)
  - UNIQUE (name, preset_type)
  - ALTER TABLE partitions ADD COLUMN indexation_preset VARCHAR DEFAULT 'default'
  - ALTER TABLE partitions ADD COLUMN retrieval_preset VARCHAR DEFAULT 'default'
  - ALTER TABLE partitions ADD COLUMN dimension INTEGER DEFAULT 1024
  - ALTER TABLE partitions ADD COLUMN chat_history_depth INTEGER DEFAULT 0
  - ALTER TABLE partitions ADD COLUMN chat_llm VARCHAR NULL
```

### 14C — Services

**`services/orchestrators/preset_service.py`:**

```python
class PresetService:
    def __init__(self, preset_repo: PresetRepository, config: Settings):
        ...

    async def seed_defaults(self) -> None:
        """Insert default indexation + retrieval presets from YAML if not in DB."""

    async def load_all(self) -> None:
        """Load all presets from DB into config.presets (runtime cache)."""

    async def get_preset(self, name: str, preset_type: str) -> PresetRow: ...
    async def list_presets(self, preset_type: str) -> list[PresetRow]: ...
    async def upsert_preset(self, preset: PresetRow) -> PresetRow: ...
    async def delete_preset(self, name: str, preset_type: str) -> bool: ...
```

**`services/orchestrators/partition_service.py`** — extend:

```python
async def load_partitions(self) -> None:
    """Load all partition configs from DB, resolve presets, merge into config.partitions."""

async def get_partition_config(self, partition: str) -> PartitionConfig:
    """Get resolved config for a partition (preset + overrides)."""
```

**Resolution chain** (how a partition gets its full config):

1. Load `PartitionRow` from DB (has `indexation_preset: str`, `retrieval_preset: str`)
2. Look up `PresetRow` for each preset name
3. Parse preset config JSON into `IndexationPipelineConfig` / `RetrievalPipelineConfig`
4. Build `PartitionConfig` with resolved pipeline configs
5. Cache in `config.partitions[name]` for fast access

### 14D — Update orchestrators to use per-partition config

**IndexingService** — currently uses global config. Change to:

```python
async def index_documents_batch(self, docs: list[Document], partition: str):
    partition_config = await self._partition_service.get_partition_config(partition)
    idx_config = partition_config.indexation    # per-partition indexation settings
    # Use idx_config.chunking.strategy, idx_config.enable_image_captioning, etc.
```

**RetrievalService** — currently uses global retriever config. Change to:

```python
async def retrieve(self, query: str, partition: str, **kwargs):
    partition_config = await self._partition_service.get_partition_config(partition)
    ret_config = partition_config.retrieval     # per-partition retrieval settings
    top_k = ret_config.top_k
    reranker_name = ret_config.reranker or "default"
    # ...
```

**QueryService** — use `partition_config.chat_llm` and `partition_config.chat_history_depth`.

### 14E — API endpoints

**`api/routers/admin/presets.py`:**

```
GET    /api/v1/admin/presets?type=indexation    # list presets
GET    /api/v1/admin/presets/{name}?type=indexation
POST   /api/v1/admin/presets                    # create/update preset
DELETE /api/v1/admin/presets/{name}?type=indexation
```

**`api/routers/admin/partitions.py`** — extend:

```
PATCH  /api/v1/admin/partitions/{name}/config   # update partition preset assignments
GET    /api/v1/admin/partitions/{name}/config    # get resolved partition config
```

**`api/routers/admin/pipelines.py`** — dry-run preview:

```
POST   /api/v1/admin/pipelines/preview          # preview pipeline config without saving
```

### 14F — Default presets (seeded from YAML)

Add to `conf/`:

```yaml
# conf/presets/indexation/default.yaml
chunking:
  strategy: recursive_splitter
  chunk_size: 512
  chunk_overlap: 64
parsing_strategy: marker
enable_image_captioning: true
enable_contextualization: false
enable_entity_extraction: true
enable_topic_tagging: true

# conf/presets/retrieval/default.yaml
type: unified
top_k: 20
top_n: 10
enable_reranker: true
enable_planner: true
rrf_k: 60
intent_strategies:
  qa:
    top_k: 10
    top_n: 5
  summarization:
    top_k: 30
    top_n: 15
```

### Commits

```
14.1   Flesh out core/config/{indexation,retrieval,partition,presets}.py
14.2   Alembic migration: presets table + partition config columns
14.3   services/persistence/preset_repo.py (implements PresetRepository)
14.4   Extend services/persistence/partition_repo.py with config columns
14.5   services/orchestrators/preset_service.py (CRUD + seed + load)
14.6   Extend services/orchestrators/partition_service.py (load_partitions, resolve presets)
14.7   Update IndexingService to use per-partition config
14.8   Update RetrievalService to use per-partition config
14.9   Update QueryService to use per-partition chat config
14.10  api/routers/admin/presets.py (CRUD endpoints)
14.11  Extend api/routers/admin/partitions.py (config endpoints)
14.12  api/routers/admin/pipelines.py (dry-run preview)
14.13  Add default preset YAML files to conf/presets/
14.14  Wire PresetService + updated PartitionService into ServiceContainer
14.15  Seed defaults in container.initialize()
14.16  Integration test: create preset -> assign to partition -> index -> retrieve
```

### What changes for existing partitions

- Existing partitions get `indexation_preset = "default"` and `retrieval_preset = "default"`
  via the migration (column defaults).
- The `"default"` preset is seeded from YAML on first startup.
- Behavior is **identical** to current OpenRAG until an admin explicitly changes a
  partition's preset or creates custom presets.
- **Zero breaking changes** — this is purely additive.

---

## Phase 15 — OIDC / Keycloak SSO Authentication

**Goal:** Add OIDC-based SSO authentication alongside the existing API token system.
Users can authenticate via Keycloak (JWT) or via API tokens (`or-` prefix) — both
methods coexist. This is a feature addition on the clean architecture.

### Architecture: Dual Auth

The auth dependency inspects the token format to dispatch:

- Starts with `eyJ` (base64 JSON) -> JWT validation path (Keycloak)
- Starts with `or-` -> DB token lookup (existing, unchanged)

Both paths produce the same `request.state.user` — downstream code is unaware
of which auth method was used.

### 15A — New files and their locations

**Core layer** (pure config, no I/O):

| File                  | Purpose                                         |
| --------------------- | ----------------------------------------------- |
| `core/config/auth.py` | Add `OIDCConfig` model to existing `AuthConfig` |

```python
class OIDCConfig(BaseModel):
    enabled: bool = False
    issuer_url: str = ""                     # https://keycloak.company.com/realms/corp
    client_id: str = ""
    client_secret: str = ""                  # only if confidential client
    audience: str | None = None              # expected "aud" claim
    claim_sub: str = "sub"                   # claim for user ID
    claim_email: str = "email"
    claim_name: str = "preferred_username"
    claim_groups: str = "groups"             # claim containing group memberships
    claim_roles: str = "resource_access.openrag.roles"
    admin_role: str = "openrag-admin"        # role that grants is_admin
    group_prefix: str = "/openrag/"          # strip from group names
    group_pattern: str = r"(.+)/(owner|editor|viewer)"
    auto_provision: bool = True              # create user on first login
    default_quota: int = 10
    jwks_cache_ttl: int = 3600               # cache JWKS keys for 1 hour
```

**Services layer** (infrastructure adapters):

| File                                | Purpose                                                     |
| ----------------------------------- | ----------------------------------------------------------- |
| `services/auth/__init__.py`         | Package init                                                |
| `services/auth/jwt_validator.py`    | Validates JWT signature against Keycloak's JWKS endpoint    |
| `services/auth/oidc_mapper.py`      | Extracts user info + partition roles from JWT claims        |
| `services/auth/oidc_provisioner.py` | Find-or-create user by `external_user_id`, sync memberships |

**API layer** (auth dependency update):

| File                       | Change                                                                                         |
| -------------------------- | ---------------------------------------------------------------------------------------------- |
| `api/dependencies/auth.py` | Add dual-auth dispatch: `_is_jwt()` detection, JWT path calls validator + mapper + provisioner |

**DI layer** (wiring):

| File              | Change                                                                                                    |
| ----------------- | --------------------------------------------------------------------------------------------------------- |
| `di/container.py` | Create `KeycloakJWTValidator`, `OIDCMapper`, `OIDCProvisioner` if OIDC enabled; inject into `AuthService` |

**Frontend** (indexer-ui):

| File                                        | Purpose                                                        |
| ------------------------------------------- | -------------------------------------------------------------- |
| `ui/src/lib/auth/oidc.ts`                   | OIDC client using `oidc-client-ts` (Authorization Code + PKCE) |
| `ui/src/routes/auth/callback/+page.svelte`  | OIDC redirect callback handler                                 |
| `ui/src/lib/components/layout/Login.svelte` | Add "Login with SSO" button alongside existing token input     |

### 15B — services/auth/jwt_validator.py

```python
class KeycloakJWTValidator:
    """Validates Keycloak-issued JWTs using OIDC discovery + JWKS."""

    def __init__(self, config: OIDCConfig):
        self._issuer = config.issuer_url
        self._audience = config.audience
        self._client_id = config.client_id
        self._jwks_client = PyJWKClient(
            f"{self._issuer}/protocol/openid-connect/certs",
            cache_keys=True,
            lifespan=config.jwks_cache_ttl,
        )

    def validate(self, token: str) -> dict:
        """Validate JWT signature and claims. Returns decoded payload."""
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            issuer=self._issuer,
            audience=self._audience or self._client_id,
        )
```

### 15C — services/auth/oidc_mapper.py

```python
class OIDCUserMapper:
    """Maps Keycloak JWT claims to OpenRAG user model."""

    def extract_user_info(self, claims: dict) -> dict:
        return {
            "external_user_id": claims[self._config.claim_sub],
            "display_name": claims.get(self._config.claim_name) or claims.get(self._config.claim_email),
            "is_admin": self._is_admin(claims),
        }

    def extract_partitions(self, claims: dict) -> list[dict]:
        """Parse Keycloak groups into partition + role pairs."""
        # /openrag/project-alpha/editor -> {"partition": "project-alpha", "role": "editor"}
```

### 15D — services/auth/oidc_provisioner.py

```python
class OIDCUserProvisioner:
    """Auto-creates/updates OpenRAG users from Keycloak claims."""

    def __init__(self, config: OIDCConfig, user_repo: UserRepository):
        ...

    async def ensure_user(self, user_info: dict, partitions: list[dict]) -> dict:
        """
        1. Lookup by external_user_id
        2. If not found + auto_provision: create user (no API token)
        3. Sync is_admin from Keycloak roles
        4. Sync partition memberships from Keycloak groups
           (Keycloak is source of truth - add missing, update changed, remove stale)
        5. Return full OpenRAG user dict
        """
```

### 15E — api/dependencies/auth.py (dual auth dispatch)

```python
async def get_current_user(request: Request, ...) -> User:
    token = _extract_token(request)

    if oidc_enabled and _is_jwt(token):           # starts with "eyJ"
        claims = jwt_validator.validate(token)
        user_info = oidc_mapper.extract_user_info(claims)
        partitions = oidc_mapper.extract_partitions(claims)
        user = await oidc_provisioner.ensure_user(user_info, partitions)
    else:                                          # starts with "or-"
        user = await auth_service.authenticate_token(token)

    return user

def _is_jwt(token: str) -> bool:
    return token.startswith("eyJ")
```

### 15F — Database changes

**No schema migration needed.** The `users.external_user_id` column already exists
(nullable, unique, indexed). OIDC users get `token = NULL` (they authenticate via JWT,
not API tokens). The `get_user_by_external_id()` method is added to `UserRepository`.

### 15G — Environment variables

```bash
# All optional - if OIDC_ENABLED is not true, OIDC auth is disabled
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://keycloak.company.com/realms/your-realm
OIDC_CLIENT_ID=openrag
OIDC_CLIENT_SECRET=                          # only if confidential client
OIDC_AUDIENCE=openrag
OIDC_ADMIN_ROLE=openrag-admin
OIDC_GROUP_PREFIX=/openrag/
OIDC_AUTO_PROVISION=true
OIDC_DEFAULT_QUOTA=10

# Frontend
VITE_OIDC_ENABLED=true
VITE_OIDC_ISSUER_URL=https://keycloak.company.com/realms/your-realm
VITE_OIDC_CLIENT_ID=openrag
```

### 15H — Frontend OIDC flow

Add `oidc-client-ts` to indexer-ui. The login page shows two options:

- "Login with SSO" button (redirects to Keycloak)
- "Login with token" input (existing behavior)

The choice is driven by `VITE_OIDC_ENABLED`. If OIDC is not configured,
only the token input is shown (no change from current behavior).

Token refresh is handled automatically by `oidc-client-ts` `automaticSilentRenew`.

### Commits

```
15.1   Add PyJWT[crypto] + oidc-client-ts to dependencies
15.2   Add OIDCConfig to core/config/auth.py
15.3   Create services/auth/jwt_validator.py
15.4   Create services/auth/oidc_mapper.py
15.5   Add get_user_by_external_id() + create_oidc_user() to UserRepository
15.6   Create services/auth/oidc_provisioner.py
15.7   Update api/dependencies/auth.py with dual-auth dispatch
15.8   Wire OIDC components into ServiceContainer (conditional on OIDC_ENABLED)
15.9   Integration test: JWT login + auto-provision + membership sync
15.10  Update indexer-ui: add oidc-client-ts, OIDC login flow, /auth/callback
15.11  Update indexer-ui: conditional login (SSO vs token based on config)
15.12  Update docker-compose + .env.example with OIDC env vars
15.13  Documentation: Keycloak setup guide (realm, client, mappers, groups)
```

### What doesn't change

- API token auth (`or-` tokens) — works exactly as before
- Role hierarchy (viewer/editor/owner) — Keycloak groups map to same roles
- SUPER_ADMIN_MODE — works with OIDC-provisioned admins
- All API endpoints — they see `request.state.user` regardless of auth method
- Database schema — no migration (external_user_id already exists)
- File quota system — applies to OIDC users same as token users

---

## Risk Register

| Risk                                            | Likelihood | Impact | Mitigation                                                                                                               |
| ----------------------------------------------- | ---------- | ------ | ------------------------------------------------------------------------------------------------------------------------ |
| **Ray actor serialization breaks**              | High       | High   | Actors are thin wrappers; domain objects use Pydantic model_dump()/model_validate() for serialization                    |
| **Import cycles**                               | Medium     | Medium | Layer guard script catches immediately; break via core/models/                                                           |
| **God object decomposition breaks integration** | High       | High   | Phase 7 uses delegation shim - old actor delegates to new stores. Test full upload-search-delete cycle after each commit |
| **Config loading order**                        | Medium     | Medium | ServiceContainer loads config once, passes down. No module-level load_config()                                           |
| **LangChain Document removal**                  | Medium     | High   | Phase 2 adds from_langchain/to_langchain. Remove only in Phase 12                                                        |
| **Async/sync mismatch**                         | Medium     | Medium | All new interfaces are async. Sync operations wrapped in asyncio.to_thread()                                             |
| **Performance regression**                      | Low        | Medium | All layers are zero-cost delegation. Profile RAG pipeline hot path                                                       |
| **Test gap during migration**                   | Medium     | High   | Run full integration suite after every commit. Add unit tests per core/ module                                           |

---

## Migration Utilities

### Import rewriter

```bash
python scripts/rewrite_imports.py --dry-run   # preview changes
python scripts/rewrite_imports.py --apply      # apply changes
```

Reads `scripts/import_mapping.json`:

```json
{
  "components.retriever": "openrag.core.retrieval.retriever",
  "components.reranker": "openrag.core.rerankers",
  "config.load_config": "openrag.core.config.loader.load_config"
}
```

### Layer import guard

```bash
python scripts/check_layer_imports.py
```

Rules:

```python
FORBIDDEN = [
    ("openrag/core/", ["openrag.services", "openrag.api", "openrag.di"]),
    ("openrag/services/", ["openrag.api"]),
]
```

### Test fixtures for new DI

```python
# tests/conftest.py
@pytest.fixture
def config():
    return load_config(overrides={"vectordb.host": "localhost"})

@pytest.fixture
async def container(config):
    c = ServiceContainer(config)
    await c.initialize()
    yield c
    await c.shutdown()

@pytest.fixture
def mock_vector_store():
    return InMemoryVectorStore()  # for unit tests
```

---

## Summary: Execution Order

```
Phase 0    Scaffold                      <- zero risk, directory creation
Phase 1    Registry & Exceptions         <- low risk, new files only
Phase 2    Domain Models                 <- low risk, new files only
Phase 3    Configuration                 <- low risk, re-export shims
Phase 4    ABCs & Ports            <- low risk, new ABCs only
-------------------------------------------------------- foundation complete
Phase 5    Core Domain Logic             <- MEDIUM, first code moves
Phase 6    Inference Adapters            <- MEDIUM, HTTP clients
Phase 7    Storage & Persistence         <- HIGH, god object decomposition
Phase 8    Orchestrators                 <- HIGH, business logic rewiring
Phase 9    Workers (Ray)                 <- HIGH, distributed system
-------------------------------------------------------- transformation complete
Phase 10   API Layer                     <- MEDIUM, router migration
Phase 11   Composition Root              <- HIGH, DI wiring
Phase 12   Internal Cleanup              <- MEDIUM, delete old code inside openrag/
-------------------------------------------------------- clean architecture complete
Phase 13   Project Layout & Infra        <- MEDIUM, top-level restructure
                                            (infra/, scripts/, tests/, ui/)
Phase 14   Per-Partition Presets         <- MEDIUM, feature addition
Phase 15   OIDC / Keycloak SSO          <- MEDIUM, dual auth (JWT + API tokens)
```

**Phase 0-4** are additive and safe - proceed rapidly.
**Phase 5-9** are the core transformation - one commit at a time, test between each.
**Phase 10-12** are the cutover - clean internal architecture.
**Phase 13** restructures everything outside `openrag/` - deployment, tests, scripts, UI.
**Phase 14** is a feature addition - per-partition presets on the clean architecture.
**Phase 15** adds OIDC SSO - Keycloak JWT alongside existing API tokens.
