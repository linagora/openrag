# Codebase Structure

**Analysis Date:** 2026-02-10

## Directory Layout

```
/home/paul/dev/linagora/server/openrag/
├── openrag/                          # Main Python package
│   ├── api.py                        # FastAPI app entry point, ray.init(), middleware setup
│   ├── chainlit_api.py               # Optional Chainlit UI server
│   ├── app_front.py                  # Chainlit frontend configuration
│   ├── consts.py                     # Constants (PARTITION_PREFIX, etc.)
│   │
│   ├── components/                   # Core RAG components
│   │   ├── pipeline.py               # RagPipeline, RetrieverPipeline orchestration
│   │   ├── retriever.py              # ABCRetriever, implementations (Single, MultiQuery, HyDE)
│   │   ├── map_reduce.py             # RAGMapReduce for large document set summarization
│   │   ├── llm.py                    # LLM client wrapper
│   │   ├── reranker.py               # Document reranking logic
│   │   ├── ray_utils.py              # call_ray_actor_with_timeout() utility
│   │   ├── utils.py                  # Distributed semaphores, context formatting, language detection
│   │   ├── prompts/                  # Prompt templates
│   │   │   ├── prompts.py            # System prompts, instruction prompts
│   │   │   └── __init__.py
│   │   │
│   │   └── indexer/                  # Document ingestion pipeline
│   │       ├── indexer.py            # Indexer Ray actor (serialize, chunk, insert orchestration)
│   │       ├── __init__.py
│   │       │
│   │       ├── loaders/              # File format converters to Document
│   │       │   ├── base.py           # BaseLoader abstract class with image captioning utilities
│   │       │   ├── serializer.py     # DocSerializer Ray actor, factory for loaders
│   │       │   ├── txt_loader.py     # Text and markdown loader
│   │       │   ├── docx.py           # Word document loader (MarkItDown)
│   │       │   ├── pptx_loader.py    # PowerPoint loader
│   │       │   ├── doc.py            # Legacy Word format loader
│   │       │   ├── image.py          # Image loader with VLM captioning
│   │       │   ├── media_loader.py   # Audio/video loader (Whisper transcription)
│   │       │   ├── eml_loader.py     # Email message loader
│   │       │   ├── CustomDocLoader.py, CustomHTMLLoader.py  # Legacy loaders
│   │       │   ├── pdf_loaders/      # PDF-specific loaders
│   │       │   │   ├── marker.py     # MarkerLoader, MarkerPool Ray actor (Marker extraction)
│   │       │   │   ├── docling2.py   # DoclingLoader2, DoclingPool Ray actor (Docling extraction)
│   │       │   │   └── __init__.py
│   │       │   └── __init__.py       # Loader factory
│   │       │
│   │       ├── chunker/              # Document splitting into chunks
│   │       │   ├── chunker.py        # BaseChunker, implementations (RecursiveSplitter, etc.)
│   │       │   ├── utils.py          # Chunk utility functions
│   │       │   ├── test_chunking.py  # Unit tests
│   │       │   └── __init__.py
│   │       │
│   │       ├── embeddings/           # Vector embedding generation
│   │       │   ├── base.py           # BaseEmbedding abstract class
│   │       │   ├── openai.py         # OpenAIEmbedding implementation (VLLM)
│   │       │   ├── __init__.py       # EmbeddingFactory
│   │       │   └── __pycache__
│   │       │
│   │       ├── vectordb/             # Vector database abstraction
│   │       │   ├── vectordb.py       # BaseVectorDB abstract, MilvusDB implementation, ConnectorFactory
│   │       │   ├── utils.py          # PartitionFileManager, Milvus schema definitions
│   │       │   └── __init__.py
│   │       │
│   │       └── utils/                # Indexer utilities
│   │           ├── files.py          # save_file_to_disk(), sanitize_filename()
│   │           ├── test_files.py     # Unit tests for file utilities
│   │           ├── text_sanitizer.py # Text cleaning utilities
│   │           ├── test_text_sanitizer.py
│   │           └── __init__.py
│   │
│   ├── routers/                      # FastAPI APIRouter handlers
│   │   ├── indexer.py                # /indexer/* endpoints (add_file, supported/types, file status)
│   │   ├── search.py                 # /search/* endpoints (semantic search by partition/file)
│   │   ├── openai.py                 # /v1/chat/completions (OpenAI-compatible chat)
│   │   ├── partition.py              # /partition/* endpoints (list, create, delete, user roles)
│   │   ├── queue.py                  # /queue/* endpoints (task status polling)
│   │   ├── extract.py                # /extract/* endpoints (get individual chunks)
│   │   ├── users.py                  # /users/* endpoints (user and membership management)
│   │   ├── tools.py                  # /v1/tools/execute (execute tools like extractText)
│   │   ├── actors.py                 # /actors/* endpoints (Ray actor introspection)
│   │   ├── utils.py                  # Shared router utilities (auth checks, partition checks)
│   │   └── __init__.py
│   │
│   ├── config/                       # Configuration system
│   │   ├── config.py                 # load_config() function using Hydra
│   │   └── __init__.py
│   │
│   ├── models/                       # Pydantic models for API requests/responses
│   │   ├── openai.py                 # OpenAIChatCompletionRequest, OpenAICompletionRequest
│   │   ├── indexer.py                # Indexer-related models
│   │   └── __pycache__
│   │
│   ├── utils/                        # Shared utilities
│   │   ├── dependencies.py           # get_or_create_actor(), dependency functions (get_vectordb, get_indexer, etc.)
│   │   ├── logger.py                 # get_logger() configures Loguru with JSON output
│   │   ├── external_resource_errors.py  # is_external_resource_error() for transient error detection
│   │   ├── exceptions/               # Custom exception hierarchy
│   │   │   ├── base.py               # OpenRAGError, EmbeddingError, VDBError base classes
│   │   │   ├── embeddings.py         # Embedding-specific exceptions
│   │   │   ├── vectordb.py           # VectorDB-specific exceptions (MilvusConnectionError, etc.)
│   │   │   ├── __init__.py
│   │   │   └── __pycache__
│   │   ├── test_logger.py            # Unit tests
│   │   ├── test_external_resource_errors.py
│   │   ├── __init__.py
│   │   └── __pycache__
│   │
│   ├── public/                       # Static assets
│   │   ├── logo_dark.png, logo_light.png
│   │   ├── favicon.svg
│   │   └── avatars/
│   │
│   ├── scripts/                      # Utility scripts
│   │   ├── embed.py                  # Bulk embedding script
│   │   ├── backup.py, backup.sh.example  # Database backup
│   │   ├── restore.py, restore.sh.example  # Database restore
│   │   ├── filter-logs.py            # Log filtering utility
│   │   ├── entrypoint-*.sh           # Docker entrypoint scripts
│   │   ├── migrations/               # Database migrations
│   │   │   └── alembic/              # Alembic migration structure
│   │   └── __init__.py
│   │
│   ├── chainlit/                     # Chainlit UI custom logic
│   │   └── [chainlit-specific files]
│   │
│   ├── test_version.py               # Version test
│   ├── __init__.py                   # Package init
│   └── __pycache__
│
├── .hydra_config/                    # Hydra configuration files
│   ├── config.yaml                   # Main config (paths, ray, llm, vectordb, loader, chunker, retriever, rag, reranker, etc.)
│   ├── chunker/
│   │   ├── base.yaml                 # Base chunker config
│   │   └── recursive_splitter.yaml   # Recursive splitter config
│   ├── retriever/
│   │   ├── base.yaml                 # Base retriever config
│   │   ├── single.yaml               # Single retriever config
│   │   ├── multiQuery.yaml           # Multi-query retriever config
│   │   └── hyde.yaml                 # HyDE retriever config
│   └── rag/
│       ├── base.yaml                 # Base RAG config
│       ├── SimpleRag.yaml            # Simple RAG mode (no chat history)
│       └── ChatBotRag.yaml           # Chat-aware RAG mode (contextualizes query)
│
├── tests/                            # Integration and E2E tests (outside openrag package)
│   ├── api_tests/
│   │   ├── mock_vllm.py              # Mock LLM/VLM endpoints for CI
│   │   └── [integration tests]
│   └── api/
│       └── [Robot Framework tests]
│
├── .env.example                      # Environment variable template
├── pyproject.toml                    # Python package metadata and dependencies
├── Makefile                          # Build/test targets
├── docker-compose.yml                # Docker services (Milvus, Redis, etc.)
├── pytest.ini                        # Pytest configuration
├── README.md                         # Project documentation
└── [other config files]
```

## Directory Purposes

**openrag/api.py:**
- Purpose: FastAPI application bootstrap and middleware setup
- Contains: Ray initialization, Hydra config loading, middleware registration, router mounting
- Key functions: custom_openapi() for security schema, AuthMiddleware.dispatch() for user auth

**openrag/components/:**
- Purpose: Core RAG business logic separated from HTTP routing
- Contains: Retrieval pipelines, LLM interactions, document processing components
- Design: Importable as pure Python (no FastAPI dependency at this level)

**openrag/components/indexer/indexer.py:**
- Purpose: Ray actor managing document indexing orchestration
- Contains: Indexer class with @ray.remote decorator, concurrency groups for serialize/chunk/insert
- Methods: `serialize_file()`, `chunk()`, `add_file()` (async), `asearch()` (retrieval delegation)

**openrag/components/indexer/vectordb/vectordb.py:**
- Purpose: Vector database abstraction layer and Milvus implementation
- Contains: BaseVectorDB abstract interface, MilvusDB concrete implementation, hybrid search logic
- Methods: `async_search()`, `async_multi_query_search()`, `async_add_documents()`, partition management

**openrag/routers/:**
- Purpose: HTTP endpoint definitions with FastAPI
- Pattern: Each file is one APIRouter; mounted at a prefix in api.py
- Dependencies: FastAPI Depends() functions for auth, partition validation, actor access

**openrag/routers/utils.py:**
- Purpose: Shared router utilities to avoid duplication
- Contains: Permission checks (require_partition_viewer, require_partition_editor), user extraction, partition filtering
- Pattern: Functions that return FastAPI Depends() callables for use in endpoint signatures

**openrag/config/:**
- Purpose: Configuration loading with Hydra and environment variable override support
- Contains: load_config() function that initializes Hydra and returns OmegaConf object
- Pattern: Hydra composes base config.yaml with selected sub-configs (chunker, retriever, rag mode)

**openrag/models/:**
- Purpose: Pydantic request/response models for validation and OpenAPI schema
- Contains: OpenAIChatCompletionRequest matching OpenAI API format, indexer request models

**openrag/utils/dependencies.py:**
- Purpose: Singleton Ray actor creation and dependency injection
- Pattern: get_or_create_actor() reuses existing actor if found, creates if missing
- Exports: get_vectordb(), get_indexer(), get_task_state_manager(), get_serializer(), get_marker_pool()

**openrag/utils/exceptions/:**
- Purpose: Custom exception hierarchy for error handling
- Contains: OpenRAGError base class, domain-specific subclasses (EmbeddingError, VDBError)
- Usage: Raised by components, caught by @app.exception_handler() in api.py → JSON response

**.hydra_config/:**
- Purpose: Configuration files for Hydra system
- Pattern: config.yaml loads defaults list to compose chunker, retriever, rag mode configs
- Overrides: Environment variables override any config value (e.g., RETRIEVER_TOP_K → retriever.top_k)

## Key File Locations

**Entry Points:**
- `openrag/api.py`: FastAPI application startup
- `openrag/chainlit_api.py`: Optional Chainlit UI server

**Configuration:**
- `.hydra_config/config.yaml`: Master configuration
- `openrag/config/config.py`: Hydra loading logic
- `.env.example`: Environment variable reference

**Core Logic:**
- `openrag/components/pipeline.py`: RAG orchestration (RagPipeline, RetrieverPipeline)
- `openrag/components/indexer/indexer.py`: Indexing orchestration (Indexer Ray actor)
- `openrag/components/indexer/vectordb/vectordb.py`: Vector storage (Milvus integration)
- `openrag/components/retriever.py`: Document retrieval strategies

**API Handlers:**
- `openrag/routers/indexer.py`: File upload, indexing status
- `openrag/routers/search.py`: Semantic search endpoints
- `openrag/routers/openai.py`: ChatCompletion endpoint (RAG generation)
- `openrag/routers/partition.py`: Partition and user management

**Testing:**
- `openrag/components/indexer/chunker/test_chunking.py`: Chunker unit tests
- `openrag/components/indexer/utils/test_files.py`: File utility tests
- `tests/api_tests/`: Integration tests (requires running server)
- `tests/api/`: Robot Framework E2E tests

## Naming Conventions

**Files:**
- Python modules: snake_case (e.g., `base_retriever.py`, `vectordb.py`)
- Test files: `test_*.py` or `*_test.py` prefix/suffix
- Config files: `.yaml` extension in `.hydra_config/`
- Scripts: descriptive verbs (e.g., `backup.py`, `embed.py`, `filter-logs.py`)

**Directories:**
- Package directories: lowercase (e.g., `components`, `routers`, `loaders`, `chunker`, `vectordb`)
- Sub-packages for concerns: `indexer/` for document ingestion, `exceptions/` for error types
- Config directories: match component names (e.g., `.hydra_config/chunker/`, `.hydra_config/retriever/`)

**Python Classes:**
- PascalCase for all classes (e.g., `Indexer`, `RagPipeline`, `BaseRetriever`, `MilvusDB`)
- Abstract base classes prefix with `ABC` or suffix with `Base` (e.g., `ABCRetriever`, `BaseLoader`, `BaseVectorDB`)
- Exception classes suffix with `Error` (e.g., `OpenRAGError`, `EmbeddingError`, `MilvusConnectionError`)
- Factory classes suffix with `Factory` (e.g., `ConnectorFactory`, `RetrieverFactory`, `ChunkerFactory`)

**Functions:**
- snake_case (e.g., `load_config()`, `get_vectordb()`, `format_context()`)
- Async functions prefix with `a` or suffix with `async` (e.g., `aload_document()`, `async_search()`)

**Variables:**
- Module-level constants: UPPERCASE (e.g., `POOL_SIZE`, `MAX_TASKS_PER_WORKER`, `ACCEPTED_FILE_FORMATS`)
- Instance/local variables: snake_case (e.g., `top_k`, `partition_name`, `retrieved_docs`)

**Types/Enums:**
- Pydantic models: PascalCase (e.g., `OpenAIChatCompletionRequest`, `SummarizedChunk`)
- Enums: PascalCase class, UPPERCASE members (e.g., `RAGMODE.SIMPLERAG`, `RAGMODE.CHATBOTRAG`)

## Where to Add New Code

**New Feature (e.g., new retrieval strategy):**
- Primary code: `openrag/components/retriever.py` - Add class inheriting from `ABCRetriever` with `async retrieve()` method
- Config: `.hydra_config/retriever/{strategy_name}.yaml` - Define configuration parameters
- Factory update: Add case to `RetrieverFactory.create_retriever()` to instantiate new strategy
- Tests: Create `openrag/components/test_retriever.py` with async test methods

**New API Endpoint (e.g., new search mode):**
- Router file: Create or update `openrag/routers/{feature}.py` with APIRouter and route handlers
- Registration: Add `app.include_router(router, prefix="/{prefix}", tags=[Tags.FEATURE])` to `openrag/api.py`
- Dependencies: Use existing dependency functions from `openrag/routers/utils.py` for auth and partition checks
- Model: Add Pydantic model to `openrag/models/` if new request/response shape needed
- Tests: Add integration test to `tests/api_tests/` with pytest (requires running server)

**New File Loader (e.g., support for new document format):**
- Implementation: Create `openrag/components/indexer/loaders/{format}_loader.py`
- Class: Inherit from `BaseLoader`, implement `async aload_document(file_path, metadata)` returning Document
- Image handling: Use `self.image_captioning` check and `self.caption_images()` method from base class
- Registration: Update `openrag/components/indexer/loaders/__init__.py` factory to import and map MIME type
- Config: Add mapping in `config.yaml` under `loader.file_loaders` and `loader.mimetypes`
- Tests: Add tests to `openrag/components/indexer/loaders/` (can be in same file as `test_*` pattern)

**New Component/Ray Actor:**
- Implementation: Create Python file in appropriate subdirectory (e.g., `openrag/components/new_actor.py`)
- Class definition: Decorate class with `@ray.remote(...)` with appropriate concurrency options
- Initialization: Set in `openrag/utils/dependencies.py` via `get_or_create_actor()` function
- Dependency: Export from dependencies module for injection into routers
- Logging: Use `get_logger()` from `openrag/utils/logger.py` with `.bind(context_keys)` for structured logging

**Utilities/Helpers:**
- Shared utilities: `openrag/components/utils.py` (for component-level utilities)
- Router utilities: `openrag/routers/utils.py` (for endpoint-level utilities, auth checks)
- Infrastructure utilities: `openrag/utils/` (for logging, exceptions, config, dependencies)
- Indexer utilities: `openrag/components/indexer/utils/` (for file handling, text sanitization)

## Special Directories

**openrag/components/indexer/loaders/pdf_loaders/:**
- Purpose: PDF extraction engines (MarkerLoader vs DoclingLoader2)
- Generated: No (source code)
- Committed: Yes
- Note: Selectable via `config.loader.file_loaders.pdf` (MarkerLoader or DoclingLoader2); each has Ray actor worker pool

**openrag/scripts/:**
- Purpose: Operational and maintenance scripts (not part of main API)
- Generated: No (source code)
- Committed: Yes
- Note: Includes backup/restore for database, embedding generation, log filtering

**openrag/scripts/migrations/alembic/:**
- Purpose: Database schema migrations (Alembic-managed)
- Generated: Versions are generated by Alembic; structure is source controlled
- Committed: Yes (all versions committed for reproducibility)

**.hydra_config/:**
- Purpose: Hydra configuration composition
- Generated: No (all YAML config files are source code)
- Committed: Yes
- Note: Can be overridden by environment variables at runtime

**logs/ (runtime):**
- Purpose: Application logs
- Generated: Yes (created at runtime by logger)
- Committed: No (.gitignore)

**data/ (runtime, if SAVE_UPLOADED_FILES=true):**
- Purpose: Uploaded file storage
- Generated: Yes (created when files are uploaded)
- Committed: No (.gitignore)

---

*Structure analysis: 2026-02-10*
