# Technology Stack

**Analysis Date:** 2026-02-10

## Languages

**Primary:**
- Python 3.12 - Main application language, configured via `requires-python = ">=3.12"` in `pyproject.toml`

**Secondary:**
- JavaScript/Node.js - Documentation site and Indexer UI (`npm i`, `npm run dev` at `extern/indexer-ui/`)
- TypeScript - Indexer UI frontend (React-based)
- YAML - Configuration via Hydra (`.hydra_config/`)

## Runtime

**Environment:**
- Python 3.12.7 - Pinned in Dockerfile and build system
- VLLM 0.9.2 - OpenAI-compatible LLM inference server (GPU and CPU variants)
- Docker - Primary deployment via `docker-compose.yaml`

**Package Manager:**
- UV (uv) - Modern Python package manager with lock file
- Lockfile: `uv.lock` (present, committed)

## Frameworks

**Core:**
- FastAPI 0.x (latest, not explicitly versioned in pyproject but required dependency)
  - HTTP API framework for OpenAI-compatible endpoints
  - Located in `openrag/api.py` (entry point with Ray initialization)
  - Routers in `openrag/routers/`

- Chainlit 2.2.1+ - Chat UI framework and data layer support
  - Mounted to FastAPI via middleware in `openrag/app_front.py` and `openrag/chainlit_api.py`
  - Supports password authentication callbacks
  - Optional UI mount controlled by `WITH_CHAINLIT_UI` env var

- Ray 2.47.1+ - Distributed computing framework for async task processing
  - Ray Actors: `Indexer`, `TaskStateManager`, `Vectordb`, `DocSerializer`, `MarkerPool`
  - Remote method calls with centralized timeout handling via `components/ray_utils.py`
  - Ray Dashboard on port 8265

**Data/Vector Database:**
- Milvus (latest via Docker) - Vector database with hybrid search (dense + BM25)
  - Python client: `pymilvus`
  - Supports async operations via `AsyncMilvusClient`
  - Hybrid search with `RRFRanker` for combining dense + sparse results

- PostgreSQL 15 - Relational database for partitions, file metadata, users, memberships
  - Accessed via `sqlalchemy` ORM
  - Connection: `postgresql://{user}:{password}@{host}:{port}/`
  - Database creation per partition: `partitions_for_collection_{collection_name}`

- SQLAlchemy 1.x (via sqlalchemy_utils) - ORM for PostgreSQL
  - Models in `openrag/components/indexer/vectordb/utils.py`: `File`, `Partition`, `User`, `PartitionMembership`
  - Migrations via `alembic` in `openrag/scripts/migrations/`

**Document Processing:**
- Marker PDF 0.2.17+ - PDF-to-Markdown with layout preservation and OCR
  - Used as default PDF loader in `.hydra_config/config.yaml`
  - Configurable via `PDFLoader` env var (alternatives: DoclingLoader, PyMuPDFLoader)
  - Pool-based processing: configurable workers and GPU allocation

- Docling 2.24.0+ - Alternative document loader supporting various formats
  - Supports PDF, DOC, DOCX, PPTX with layout understanding

- MarkItDown 0.1.3+ (with DOCX support) - Office document conversion to Markdown
  - Used by `DocxLoader`, `PPTXLoader`, `DocLoader`

- PyMuPDF4LLM 0.0.17+ - Alternative PDF processing library

- HTML-to-Markdown 2.4+ - HTML content conversion

- Eml-Parser 2.0+ - Email message parsing for `.eml` files

**Embeddings & LLM:**
- OpenAI SDK 1.64.0+ - OpenAI API compatibility layer
  - Used for embeddings via `openai.embeddings.create()`
  - LLM completions via OpenAI-compatible API
  - Located in `openrag/components/indexer/embeddings/openai.py` and `openrag/components/llm.py`

- LangChain ecosystem:
  - `langchain-core` 0.3.39+
  - `langchain-community` 0.3.18+
  - `langchain-milvus` 0.1.8+ - Milvus integration
  - `langchain-openai` 0.3.7+ - OpenAI integration
  - `langchain-huggingface` 0.1.2+ - HuggingFace model support
  - `langchain-experimental` 0.3.4+ - Experimental features
  - Used for document splitting, embeddings, LLM orchestration

**Retrieval & Search:**
- Reranker API (Alibaba-NLP/gte-multilingual-reranker-base by default)
  - Base URL: `http://reranker:7997` (configurable)
  - Client: `infinity-client` 0.0.76+
  - Optional: Can be disabled via `RERANKER_ENABLED` env var

- Language detection:
  - `langdetect` 1.0.9
  - `fast-langdetect` 1.0+ (faster variant)

**Audio/Video Processing:**
- Librosa 0.11.0 - Audio analysis and feature extraction
- PyDub 0.25.1 - Audio conversion and processing
- Spire.Doc 13.1.0 - Document manipulation
- FFmpeg (system-level, installed in Dockerfile) - Audio/video codec support

**ML/Vectorization:**
- Torch 2.4.1+ - PyTorch for ML operations
- UMAP 0.5.9+ - Dimensionality reduction
- HDBSCAN 0.8.40+ - Clustering algorithm
- Numba 0.61.2+ - JIT compilation for performance
- LLVMLITE 0.44.0+ - LLVM binding (dependency for Numba)

**Configuration & Environment:**
- Hydra 1.3.2+ - Configuration management framework
  - Config files: `.hydra_config/config.yaml` and subdirectories (chunker/, retriever/, rag/)
  - Supports environment variable overrides via `${oc.env:VAR_NAME}`
  - Located at `openrag/config.py` with `load_config()`

- Python-dotenv 1.0.1+ - `.env` file loading

**Testing:**
- Pytest 8.4.1+ - Test runner
- Pytest-asyncio 1.3.0+ - Async test support
- Pytest-env 1.1.5+ - Environment variable injection for tests
- Robot Framework 7.2.2+ - BDD/acceptance testing
- Robot Framework Requests 0.9.7+ - HTTP library for Robot tests

**Logging:**
- Loguru 0.7.3+ - Structured logging
  - Replaces standard `logging`
  - Configured via `LOG_LEVEL` env var
  - Context binding for request correlation

**Development/Linting:**
- Ruff 0.14.1+ - Python linter and formatter
  - Config in `pyproject.toml` [tool.ruff] section
  - Line length: 120, Python 3.12 target
  - Rules: E, W, F, I, C4, UP, PIE

**Database Migrations:**
- Alembic 1.17.0+ - SQLAlchemy migrations
  - Located in `openrag/scripts/migrations/alembic/`

**HTTP Client:**
- httpx (latest) - Async HTTP client
  - Used in `openrag/components/llm.py` for OpenAI API calls
  - Supports streaming and timeouts

**Async Utilities:**
- AsyncPG 0.30.0+ - PostgreSQL async driver (installed but integration managed via SQLAlchemy)

**Character Encoding:**
- Chardet 5.2.0+ - Character encoding detection

**System Utilities:**
- PSUtil 7.0+ - Process and system monitoring

**SVG Processing:**
- CairoSVG 2.7+ - SVG rendering to raster formats
  - Requires Cairo libraries (installed in Dockerfile)

**Process Management:**
- Psutil 7.0.0+ - System and process utilities

## Configuration

**Environment:**
- Configured via environment variables with Hydra interpolation
- `.env` file support (loaded at startup)
- Key variables in `.env.example` (not reading secrets per policy)

**Build:**
- Dockerfile uses uv-based Python 3.12 installation
- Docker Compose with GPU and CPU profiles
- Shared environment variables via `SHARED_ENV` mount
- Model weights cached at `~/.cache/huggingface` (mounted in containers)

**Configuration Files:**
- `.hydra_config/config.yaml` - Main configuration
- `.hydra_config/chunker/*.yaml` - Document chunking strategies
- `.hydra_config/retriever/*.yaml` - Retrieval methods (single, multiQuery, hyde)
- `.hydra_config/rag/*.yaml` - RAG mode configurations (SimpleRag, ChatBotRag)
- `.venv/` - Python virtual environment directory

## Platform Requirements

**Development:**
- Python 3.12 (managed via uv)
- Docker + Docker Compose with GPU support (NVIDIA runtime optional)
- HUGGINGFACE_TOKEN for model downloads (optional, for HuggingFace models)
- Recommended: NVIDIA GPU with CUDA support for accelerated processing

**Production:**
- Docker containers (GPU or CPU configuration)
- Kubernetes support via Helm charts (`charts/openrag-stack/`)
- Distributed Ray cluster capable of running async actors
- PostgreSQL 15+ database
- Milvus vector database instance
- VLLM inference service (or compatible OpenAI-compatible API)
- Optional Reranker service (HTTP API compatible with infinity-client)
- Optional Transcriber service for audio processing
- Memory: 10.24GB shared memory for Docker containers minimum

**Network:**
- FastAPI port (default 8080)
- Ray Dashboard port (default 8265)
- Chainlit UI port (default 8090)
- Indexer UI port (default 3042)
- Internal service communication: Milvus (19530), PostgreSQL (5432), VLLM (8000), Reranker (7997)

---

*Stack analysis: 2026-02-10*
