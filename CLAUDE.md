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
uv run ruff check openrag/ tests/
uv run ruff format openrag/ tests/
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
- `TaskStateManager` (`openrag/components/indexer/indexer.py`) - Tracks async task states: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED (or FAILED or CANCELLED)
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

### Source Citation Filtering

The RAG pipeline filters out false-positive sources by having the LLM self-report which sources it actually used:

1. `format_context()` (`openrag/components/utils.py`) numbers each source (`[Source 1]`, `[Source 2]`, ...) in the context and returns `(formatted_text, included_indices)` — the indices track which docs fit within the token budget
2. Prompt templates (`prompts/example1/*.txt`) instruct the LLM to append `[Sources: 1, 3, 5]` at the end of its response
3. `extract_and_strip_sources_block()` strips this tag from the response before sending to the client
4. `filter_sources_by_citations()` filters the source metadata to only include cited sources (falls back to all sources if none match)
5. For streaming, the OpenAI router buffers the last 100 chars to catch the sources tag before it reaches the client

The `extra` field in API responses is a JSON string: `{"sources": [filtered_source_list]}`.

### API Routers (`openrag/routers/`)

- `openai.py` - OpenAI-compatible `/v1/chat/completions` endpoint
- `indexer.py` - Document ingestion endpoints
- `search.py` - Semantic search endpoints
- `partition.py` - Partition management (multi-tenant document collections)
- `users.py` - User and membership management
- `queue.py` - Task queue monitoring
- `tools.py` - Tools like `extractText` at `/v1/tools/execute` (tool param requires JSON: `{"name": "extractText"}`)

### User Management & Authentication

The system uses token-based authentication with role-based access control (RBAC) for multi-tenant partition access.

**Database Schema** (PostgreSQL with SQLAlchemy, in `openrag/components/indexer/vectordb/utils.py`):
- `users` - User accounts with `id`, `external_user_id`, `display_name`, `token` (SHA-256 hashed), `is_admin`, `file_quota`, `file_count`
- `files` - File records with `file_id`, `partition_name`, `file_metadata`, `created_by` (FK to users), `relationship_id`, `parent_id`
- `partition_memberships` - Join table linking users to partitions with roles (`owner`, `editor`, `viewer`)
- `partitions` - Document collections with cascade delete to files and memberships

**Authentication Flow** (`openrag/api.py` - `AuthMiddleware`):
1. Token extracted from `Authorization: Bearer <token>` header (or `?token=` query param for `/static` routes)
2. Token hashed with SHA-256, looked up in database
3. User info and accessible partitions set on `request.state.user` and `request.state.user_partitions`
4. Bypassed for: `/docs`, `/openapi.json`, `/redoc`, `/health_check`, `/version`, `/chainlit/*`
5. If `AUTH_TOKEN` env var is not set, defaults to admin user (id=1) for all requests

**Role Hierarchy** (`openrag/routers/utils.py`):
```python
ROLE_HIERARCHY = {"viewer": 1, "editor": 2, "owner": 3}
```

**Permission Dependencies** (`openrag/routers/utils.py`):
- `require_admin` - User must have `is_admin=True`
- `require_partition_viewer` / `require_partition_editor` / `require_partition_owner` - Check partition membership role
- `SUPER_ADMIN_MODE=true` env var allows admin users (`is_admin=True`) to bypass partition checks; regular users remain restricted to their partition memberships

**User API Endpoints** (`/users/`):
| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/users/` | GET | Admin | List all users |
| `/users/info` | GET | Any | Get current user info |
| `/users/` | POST | Admin | Create user (returns token once) |
| `/users/{user_id}` | DELETE | Admin | Delete user (cannot delete id=1) |
| `/users/{user_id}/regenerate_token` | POST | Admin/self | Regenerate API token |
| `/users/{user_id}/quota` | PATCH | Admin | Update user file quota |

**Partition Membership Endpoints** (`/partition/{partition}/users`):
| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/partition/{partition}/users` | GET | Owner | List partition members |
| `/partition/{partition}/users` | POST | Owner | Add user with role |
| `/partition/{partition}/users/{user_id}` | DELETE | Owner | Remove user |
| `/partition/{partition}/users/{user_id}` | PATCH | Owner | Update user role |

**Core Implementation** (`PartitionFileManager` in `openrag/components/indexer/vectordb/utils.py`):
```python
# User operations (called via MilvusDB Ray actor)
await vectordb.create_user.remote(display_name="Name", is_admin=False)
await vectordb.get_user_by_token.remote(token)
await vectordb.regenerate_user_token.remote(user_id)

# Membership operations
await vectordb.add_partition_member.remote(partition, user_id, role="editor")
await vectordb.update_partition_member_role.remote(partition, user_id, "owner")
await vectordb.list_partition_members.remote(partition)
```

**Token Format**: `"or-" + secrets.token_hex(16)` (34-char string, shown only once on creation/regeneration)

**Bootstrap**: On startup, ensures admin user (id=1) exists using `AUTH_TOKEN` env var or generates a random token.

**Multi-Partition Search**: Users can search across all their accessible partitions:
- Search endpoint: `GET /search?partitions=all&text=query`
- Chat completions: `POST /v1/chat/completions` with `"model": "openrag-all"`
- For regular users, `all` resolves to their partition memberships only
- For admins with `SUPER_ADMIN_MODE=true`, `all` resolves to all system partitions
- Model prefix is `openrag-` (legacy: `ragondin-`)

### Web Search Integration

Optional web search augmentation via the Staan API, allowing the LLM to combine RAG document context with live web results.

**Configuration** (`.hydra_config/config.yaml` → `websearch:` block, env vars):
- `WEBSEARCH_API_TOKEN` — provider API token; if unset, web search is silently disabled
- `WEBSEARCH_BASE_URL` — provider endpoint (default: Staan API)
- `WEBSEARCH_TOP_K` — number of web results (default: 5)
- `WEBSEARCH_LANG` — search language/market (default: `fr-FR`)

**How it works:**
- Client sends `metadata: {"websearch": true}` in the chat completion request
- **Combined mode** (partition + websearch): RAG retrieval and web search run concurrently via `asyncio.gather()`; web results are appended after document sources with continuous `[Source N]` numbering
- **Web-only mode** (no partition + websearch): skips RAG retrieval entirely, uses web results as sole context; if no results (token unset / search fails), falls back to plain direct LLM mode
- Source entries include `source_type: "document"` or `source_type: "web"` in the `extra.sources` response

**Key files:**
- `openrag/components/websearch/` — `WebSearchService`, `BaseWebSearchProvider`, `StaanProvider`
- `openrag/components/utils.py` — `format_web_context()` formats web results as numbered source blocks
- `openrag/components/pipeline.py` — `_prepare_for_web_only()`, web search logic in `_prepare_for_chat_completion()`
- `openrag/routers/openai.py` — `__prepare_sources()` merges document and web sources

### File Quota System

Per-user file quota enforcement tracked via the `file_count` and `file_quota` columns on `users`, and `created_by` on `files`.

**How it works:**
- `files.created_by` records which user uploaded each file (nullable for pre-migration files)
- `users.file_count` is incremented/decremented in application code (in `PartitionFileManager`) — no SQL triggers
- Decrements use `func.greatest(file_count - N, 0)` to prevent negative values from race conditions
- `delete_partition` queries per-uploader counts before cascade delete, then bulk decrements
- Quota check (`check_user_file_quota` in `openrag/routers/utils.py`) runs on upload, considering both indexed files and pending tasks

**Quota logic (`file_quota` column):**
- `None` → use global default (`DEFAULT_FILE_QUOTA` env var, default `-1`)
- `< 0` → unlimited
- `>= 0` → specific limit
- Admins always bypass quota checks

**Key design decisions:**
- Counts are tracked per **uploader** (whoever calls the upload API), not per partition owner
- `created_by` uses `ondelete="SET NULL"` so deleting a user doesn't cascade-delete their files
- `Indexer.delete_file` and `MilvusDB.delete_file/delete_partition` don't need a `user_id` parameter — the uploader is looked up from `files.created_by`

**Migration:** `openrag/scripts/migrations/alembic/versions/c224d4befe71_add_file_count_and_file_quota.py`

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

**Mock VLLM for CI:** `tests/api_tests/api_run/mock_vllm.py` provides fake embeddings and completions endpoints (streaming and non-streaming) for testing without a real LLM. Pydantic request models use `ConfigDict(extra="allow")` to accept vendor-specific fields like `extra_body`.

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
