# Architecture

**Analysis Date:** 2026-02-10

## Pattern Overview

**Overall:** Layered distributed architecture with Ray for async task processing and FastAPI for HTTP API

**Key Characteristics:**
- Ray Actor-based distributed computing for indexing, vectordb, and task management
- Partition-based multi-tenant document organization
- Pipeline-based retrieval and RAG generation with composable components
- Hybrid search combining dense embeddings with BM25 sparse search
- Factory pattern for pluggable loaders, chunkers, retrievers, and embedders

## Layers

**API Layer (Routers):**
- Purpose: HTTP endpoint handling, request validation, authentication, and response formatting
- Location: `openrag/routers/`
- Contains: FastAPI APIRouter instances for indexer, search, OpenAI-compatible API, partition, queue, users, tools, extract, actors
- Depends on: Components layer (pipeline, indexer, vectordb), dependency injection system
- Used by: External clients, frontend applications

**Component Layer (Business Logic):**
- Purpose: Core RAG pipeline orchestration, document processing, and retrieval logic
- Location: `openrag/components/`
- Contains: Pipeline classes (`RagPipeline`, `RetrieverPipeline`), loaders, chunkers, embedders, rerankers, retrievers, map-reduce
- Depends on: Ray actors, config system, LLM clients, vectordb
- Used by: Routers, each other

**Ray Actor Layer (Distributed State):**
- Purpose: Stateful distributed services with concurrency management and task state tracking
- Location: `openrag/components/indexer/indexer.py`, `openrag/components/indexer/vectordb/vectordb.py`, `openrag/utils/dependencies.py`
- Contains:
  - `Indexer` actor: File serialization, chunking, and insertion coordination (concurrency groups: serialize, chunk, insert, search, delete, update)
  - `Vectordb` actor: Vector storage and retrieval (MilvusDB implementation)
  - `TaskStateManager` actor: Async task state tracking (QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED/FAILED)
  - `DocSerializer` actor: File-to-Document conversion using loaders
  - `MarkerPool`/`DoclingPool` actors: Worker pools for PDF processing
- Depends on: Config system, logger, external services (Milvus, LLM, VLM)
- Used by: Router handlers, component layer

**Data Processing Pipeline (Indexing Flow):**
- Purpose: Sequential processing of uploaded documents
- Location: `openrag/components/indexer/loaders/`, `openrag/components/indexer/chunker/`, `openrag/components/indexer/embeddings/`
- Contains:
  - Loaders: Convert files to markdown Documents (MarkerLoader, DocxLoader, ImageLoader, etc.)
  - Chunker: Split documents into overlapping chunks with metadata
  - Embedder: Generate vector embeddings via VLLM
  - VectorDB: Insert chunks with partition-based organization
- Depends on: Ray, external LLM/VLM services, Milvus
- Used by: Indexer actor

**Retrieval & Generation Pipeline:**
- Purpose: Query-to-response flow for RAG
- Location: `openrag/components/pipeline.py`, `openrag/components/retriever.py`, `openrag/components/map_reduce.py`
- Contains:
  - `RetrieverPipeline`: Orchestrates retrieval and optional reranking
  - `Retriever` implementations: BaseRetriever, SingleRetriever, MultiQueryRetriever, HydeRetriever
  - `RagPipeline`: Handles query contextualization, RAG mode selection, and LLM generation
  - `RAGMapReduce`: Map-reduce summarization for large document sets
- Depends on: Vectordb actor, LLM client, reranker
- Used by: OpenAI-compatible API router

**Infrastructure Layer:**
- Purpose: Utilities and cross-cutting concerns
- Location: `openrag/utils/`, `openrag/config/`
- Contains:
  - `dependencies.py`: Actor initialization and dependency injection
  - `exceptions/`: Custom error hierarchy (OpenRAGError, VDBError, EmbeddingError)
  - `logger.py`: Structured logging with Loguru
  - `config/`: Hydra configuration loading and environment override
  - Distributed semaphores for LLM, VLM, and audio concurrent access control
- Depends on: Ray, FastAPI, external services
- Used by: All layers

## Data Flow

**Document Ingestion (Indexing):**

1. File uploaded to `/indexer/add_file` endpoint
2. Router saves file to disk, creates file_id, calls Indexer.add_file()
3. Indexer.add_file() calls:
   - TaskStateManager.set_details() → task marked QUEUED
   - handle.serialize_file() → DocSerializer actor converts file to Document (SERIALIZING)
   - handle.chunk() → Chunker splits document into chunks (CHUNKING)
   - vectordb.async_add_documents() → Embedder generates vectors, Milvus inserts chunks with partition (INSERTING)
   - Task marked COMPLETED (or FAILED on error)
4. Client polls `/queue/tasks/{task_id}` for completion status

**Semantic Search (Retrieval):**

1. Client calls `/search` or `/search/partition/{partition}` with query text and top_k
2. Router extracts user partitions via auth middleware (request.state.user_partitions)
3. Calls Indexer.asearch() → delegates to Vectordb.async_search()
4. Vectordb:
   - Embeds query via VLLM
   - Executes hybrid search: dense AnnSearch + BM25 sparse search with RRF ranking
   - Optionally retrieves surrounding chunks
   - Returns top_k ranked Documents with metadata
5. Router transforms Documents to JSON response with links to chunks

**RAG Generation:**

1. Client calls `/v1/chat/completions` with messages array (ChatBotRag) or query (SimpleRag)
2. RagPipeline.generate_query() contextualizes based on RAG mode:
   - SimpleRag: Returns last user message as-is
   - ChatBotRag: Calls LLM to generate standalone query from chat history
3. RetrieverPipeline.retrieve_docs():
   - Calls appropriate retriever (SingleQuery, MultiQuery, or HyDE)
   - Reranks results if enabled
   - Returns top_k Documents
4. Check document count:
   - If > max_map_reduce_docs: Use RAGMapReduce to summarize each chunk
   - Else: Format documents as context
5. RagPipeline calls LLM with formatted context and system prompt
6. Stream or return completion response

**State Management:**

- Task state: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED/FAILED
- Stored in TaskStateManager Ray actor (in-memory, not persisted)
- Client retrieves via polling `/queue/tasks/{task_id}`

## Key Abstractions

**Ray Actor:**
- Purpose: Provides stateful, distributed services with isolation and concurrency control
- Examples: `Indexer`, `Vectordb`, `TaskStateManager`, `MarkerPool`
- Pattern: Remote method calls with `@ray.method(concurrency_group=...)` decorators; lifetime="detached" for persistence across client connections

**File Loader:**
- Purpose: Convert various file formats to standardized Document with markdown content
- Examples: `BaseLoader` (abstract), `MarkerLoader` (PDF), `DocxLoader` (WORD), `ImageLoader` (Vision), `VideoAudioLoader` (Audio)
- Pattern: All inherit from `BaseLoader` in `openrag/components/indexer/loaders/base.py`; implement `async aload_document(file_path, metadata)` returning Document with page_content and metadata dict
- Configuration: Activated via `config.loader.file_loaders` mapping MIME type → loader class

**Document Chunk:**
- Purpose: Represents a portion of a document with metadata for vector search
- Pattern: LangChain's `Document` object with `page_content` (text) and `metadata` dict containing file_id, partition, page, chunk_index, source, timestamps, etc.

**Retriever:**
- Purpose: Strategy pattern for different document retrieval approaches
- Examples: `BaseRetriever`, `SingleRetriever`, `MultiQueryRetriever`, `HydeRetriever`
- Pattern: All inherit from `ABCRetriever`; implement `async retrieve(partition, query, filter)` returning list of Documents
- Factory: `RetrieverFactory.create_retriever(config)` based on `config.retriever.type`

**RAG Mode:**
- Purpose: Configurable query generation strategy
- Enum: `RAGMODE.SIMPLERAG` vs `RAGMODE.CHATBOTRAG`
- Pattern: Selected via `config.rag.mode`; affects whether chat history is contextualized before retrieval

**Partition:**
- Purpose: Logical document collection for multi-tenancy and access control
- Pattern: String identifier stored in chunk metadata; Milvus partitions organize chunks; separate user role permissions per partition
- Examples: "_default", "project_A", "customer_B"

**Distributed Semaphore:**
- Purpose: Rate-limiting concurrent calls to external services (LLM, VLM, audio transcription)
- Pattern: Ray actor with asyncio.Semaphore; context manager `async with get_llm_semaphore():`
- Instances: `llmSemaphore`, `vlmSemaphore`, `audioSemaphore` (created at module load time)

## Entry Points

**FastAPI Application:**
- Location: `openrag/api.py`
- Triggers: uvicorn server startup (dev: reload=True; prod: Ray Serve deployment)
- Responsibilities:
  - Initializes Ray (`ray.init()` before imports)
  - Loads Hydra config (`config = load_config()`)
  - Registers middlewares: AuthMiddleware (user/partition extraction), CORS
  - Registers 9 API routers at various prefixes
  - Defines exception handler for OpenRAGError → JSON response

**Chainlit UI Entry Point:**
- Location: `openrag/chainlit_api.py`
- Triggers: Optional (WITH_CHAINLIT_UI env var); mounted as separate app
- Responsibilities: Chainlit UI server for chat interface; uses OpenAI router endpoints

**Document Upload:**
- Endpoint: `POST /indexer/add_file`
- Location: `openrag/routers/indexer.py` → `add_file()` function
- Responsibilities:
  - Validates file format against ACCEPTED_FILE_FORMATS
  - Saves file to `{DATA_DIR}/files/{file_id}` (if SAVE_UPLOADED_FILES=true)
  - Extracts metadata from request (file_id, custom metadata)
  - Calls `indexer.add_file()` Ray actor method
  - Returns task_id for client to poll status

**Chat Completion:**
- Endpoint: `POST /v1/chat/completions`
- Location: `openrag/routers/openai.py` → `chat_completions_handler()` function
- Responsibilities:
  - Validates model ID contains partition name
  - Calls `RagPipeline.generate_query()` → retrieval → LLM
  - Streams or batches response based on stream param
  - Returns OpenAI-compatible response format

## Error Handling

**Strategy:** Custom exception hierarchy with HTTP status codes and detail messages

**Patterns:**

- **Base Exception:** `OpenRAGError` (all custom exceptions inherit; has message, code, status_code, extra dict)
- **Subclass Structure:**
  - `EmbeddingError`: Embedding generation failures (VLM/LLM timeouts, API errors)
  - `VDBError`: Vector database errors (connection, query, schema)
  - More specific subclasses: `MilvusConnectionError`, `ChunkInsertionError`, `InvalidPartitionError`, etc.
- **Ray Task Failures:**
  - Caught in router handlers as `RayTaskError`
  - Wrapped in OpenRAGError for client response
  - Logged via `logger.bind(...)` with context
- **Validation Errors:**
  - File format validation, metadata schema validation
  - Return 400 Bad Request via custom exceptions
- **Authentication Errors:**
  - Invalid/missing token returns 403 Forbidden via AuthMiddleware
  - No partition access returns 403 via `require_partition_viewer` dependency
- **External Service Errors:**
  - LLM/VLM timeouts: Exponential backoff, max_retries=3
  - Network errors: Wrapped in custom exceptions with retry guidance
  - Milvus unavailable: Returns 503 Service Unavailable

## Cross-Cutting Concerns

**Logging:**
- Framework: Loguru (structured JSON logging)
- Pattern: `logger = get_logger()` returns configured logger; use `.bind(key=value)` to attach context (file_id, partition, task_id, user_id)
- Example: `logger.bind(file_id=file_id, partition=partition).info("Queued file for indexing.")`
- Output: Logs to `{LOG_DIR}/app.json` (configured in config.yaml)

**Validation:**
- File format: `validate_file_format()` in `openrag/routers/utils.py` checks against ACCEPTED_FILE_FORMATS
- Metadata: `validate_metadata()` ensures required fields (file_id, source, etc.)
- Partition access: `require_partition_viewer()`, `require_partition_editor()` FastAPI dependencies verify user roles
- Query constraints: `top_k` bounded by config; `similarity_threshold` normalized to [0, 1]

**Authentication:**
- Mechanism: Bearer token in Authorization header (optional if AUTH_TOKEN not set; defaults to user_id=1)
- Flow: AuthMiddleware calls `vectordb.get_user_by_token.remote(token)` → retrieves user and partitions → attaches to request.state
- Partitions: User can only access partitions where they have explicit role (viewer, editor)
- Admin bypass: Super admins (user role="admin") can access all partitions

**Concurrency Control:**
- Ray actor method concurrency groups: Indexer has separate limits for serialize, chunk, insert, search, delete, update
- Distributed semaphores: LLM semaphore (llmSemaphore max concurrent), VLM semaphore (vlmSemaphore), audio semaphore
- Async/await: All I/O operations are async; Ray actor methods are concurrent but single-threaded per actor
- Task retries: `max_task_retries=3` for Indexer actor on transient failures

**Configuration:**
- System: Hydra YAML-based with environment variable overrides
- Location: `openrag/config/config.py` → loads from `.hydra_config/config.yaml`
- Overrides: Any config value can be overridden via env var matching the path (e.g., `LLM_BASE_URL` → `llm.base_url`)
- Composition: Base config + specific configs for chunker, retriever, RAG mode via `defaults` key
- Access: `config = load_config()` returns OmegaConf object; access via dot notation `config.llm.api_key`

---

*Architecture analysis: 2026-02-10*
