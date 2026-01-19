# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenRag is a modular Retrieval-Augmented Generation (RAG) framework built with FastAPI, Ray for distributed computing, and Milvus as the vector database. It provides document ingestion, chunking, embedding, and retrieval capabilities with an OpenAI-compatible API.

## Common Commands

### Development

```bash
# Install dependencies
uv sync

# Run the application locally (requires Docker services)
docker compose up -d           # GPU deployment
docker compose --profile cpu up -d  # CPU deployment

# Run with rebuild for development
docker compose up --build -d
```

### Testing

```bash
# Run all unit tests
uv run pytest

# Run a single test file
uv run pytest openrag/components/indexer/chunker/test_chunking.py

# Run tests matching a pattern
uv run pytest -k "test_chunk"
```

### Linting

```bash
uv run ruff check openrag/
uv run ruff format openrag/
```

### Documentation Site

```bash
npm i
npm run dev  # Start dev server at http://localhost:4321/openrag
```

## Architecture

### Core Components

The main application entry point is `openrag/api.py` which creates a FastAPI app with Ray initialization.

**Ray Actors** (distributed components):
- `Indexer` (`openrag/components/indexer/indexer.py`) - Handles document ingestion, chunking, and insertion into vector DB
- `TaskStateManager` (`openrag/components/indexer/indexer.py`) - Tracks async task states: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED (or FAILED)
- `Vectordb` / `MilvusDB` (`openrag/components/indexer/vectordb/vectordb.py`) - Vector database operations with hybrid search (dense + BM25 sparse)
- `DocSerializer` - Serializes files to Document objects using appropriate loaders
- `MarkerPool` / `MarkerWorker` - Pool of workers for PDF processing with Marker

**Pipeline Classes**:
- `RagPipeline` (`openrag/components/pipeline.py`) - Orchestrates retrieval and LLM generation
- `RetrieverPipeline` - Handles document retrieval and reranking
- `RAGMapReduce` (`openrag/components/map_reduce.py`) - Map-reduce for processing large document sets

### Document Processing Flow

1. Files uploaded via `/indexer/add_file` endpoint
2. `Indexer.add_file()` serializes file to Document using appropriate loader
3. Chunker splits document into chunks with contextual metadata
4. Embedder generates vectors via VLLM (OpenAI-compatible API)
5. Chunks inserted into Milvus with partition-based organization

### File Loaders (`openrag/components/indexer/loaders/`)

Each file type has a dedicated loader that converts to markdown:
- `MarkerLoader` (default for PDF, in `pdf_loaders/`) - Supports OCR, complex layouts, tables
- `DocxLoader`, `PPTXLoader`, `DocLoader` - Office formats (uses MarkItDown library)
- `ImageLoader` - VLM-powered image captioning
- `VideoAudioLoader` - Audio transcription via Whisper
- `MarkdownLoader`, `TextLoader` (`txt_loader.py`) - Markdown and plain text files

**Loader base class:** All loaders inherit from `BaseLoader` (`base.py`) which provides:
- `self.image_captioning` - whether image captioning is enabled (use this, not `self.config.loader["image_captioning"]`)
- `self.config` - Hydra config access
- `get_image_description(image_data)` - Low-level VLM captioning (accepts PIL Image, HTTP URL, or data URI)
- `caption_images(images, desc)` - Caption a list of PIL images concurrently with progress bar
- `replace_markdown_images_with_captions(content, ...)` - Find and replace markdown image references with captions
- Class regex patterns: `HTTP_IMAGE_PATTERN`, `DATA_URI_IMAGE_PATTERN`

**Loader image captioning pattern:** Loaders that process images must check `self.image_captioning` before captioning. Use the shared methods above rather than duplicating captioning logic. Access additional loader config via `self.config.loader.get("option_name", default)`.

**Image handling approaches:**
- PDF/DOCX/PPTX: Extract binary image data from file, pass to VLM directly
- Markdown: Parse image URLs from text; HTTP URLs require `IMAGE_CAPTIONING_URL=true`

### API Routers (`openrag/routers/`)

- `openai.py` - OpenAI-compatible `/v1/chat/completions` endpoint
- `indexer.py` - Document ingestion endpoints
- `search.py` - Semantic search endpoints
- `partition.py` - Partition management (multi-tenant document collections)
- `users.py` - User and membership management
- `queue.py` - Task queue monitoring
- `tools.py` - Tools like `extractText` at `/v1/tools/execute` (tool param requires JSON: `{"name": "extractText"}`)

### Configuration

Configuration uses Hydra with YAML files in `.hydra_config/`:
- Main config: `.hydra_config/config.yaml`
- Chunker configs: `.hydra_config/chunker/`
- Retriever configs: `.hydra_config/retriever/`
- RAG mode configs: `.hydra_config/rag/`

Environment variables override config values (see `.env.example`).

### Testing Structure

- Unit tests: `openrag/components/**/test_*.py` (pytest)
- API integration tests: `tests/api_tests/*.py` (pytest, requires running server)
- Robot Framework tests: `tests/api/*.robot`
- Test config in `pytest.ini` sets `CONFIG_PATH` and `PROMPTS_DIR`

**Running integration tests locally with act:**
```bash
# Run API tests using GitHub Actions locally
act -j api-tests -W .github/workflows/api_tests.yml --bind
```

**Mock VLLM for CI:** `.github/workflows/api_tests/mock_vllm.py` provides fake embeddings and completions endpoints for testing without a real LLM.

## Key Patterns

### Ray Actor Access

```python
# Get actor references
vectordb = ray.get_actor("Vectordb", namespace="openrag")
indexer = ray.get_actor("Indexer", namespace="openrag")
task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")

# Call remote methods
await vectordb.async_search.remote(query=query, partition=partition)
```

### Ray Actor Timeout and Cancellation

Use the centralized utility for calling Ray actors with proper timeout and cancellation handling:

```python
from components.ray_utils import call_ray_actor_with_timeout

result = await call_ray_actor_with_timeout(
    future=actor.method.remote(args),
    timeout=TIMEOUT_SECONDS,
    task_description="Description for error messages",
)
```

This handles:
- Timeout with `ray.wait()` and `ray.cancel()`
- `asyncio.CancelledError` propagation
- `RayTaskError` and `TaskCancelledError` handling

### Custom Exceptions

All custom exceptions inherit from `OpenRAGError` (`openrag/utils/exceptions/`):
- `VDBError` subclasses for vector database errors
- `EmbeddingError` for embedding failures

### Logging

Uses Loguru with structured logging:
```python
from utils.logger import get_logger
logger = get_logger()
logger.bind(file_id=file_id, partition=partition).info("Message")
```

### Import Conventions

Use absolute imports from the `openrag/` directory (which is the Python path root):
```python
# Correct - absolute imports
from components.ray_utils import call_ray_actor_with_timeout
from utils.logger import get_logger
from config import load_config

# Avoid relative imports across packages
# from .ray_utils import ...  # Only within same package
```
