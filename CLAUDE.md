# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenRag is a modular Retrieval-Augmented Generation (RAG) framework built with FastAPI, Ray for distributed computing, and Milvus as the vector database. It provides document ingestion, chunking, embedding, and retrieval capabilities with an OpenAI-compatible API.

## Project Layout

```text
openrag/        # Python package (application code only): core/ services/ api/ di/ prompts/
conf/           # YAML configuration
infra/          # All deployment infrastructure
  docker/       #   api.Dockerfile, ray.Dockerfile, ui.Dockerfile (build from repo root)
  compose/      #   docker-compose.yaml + service configs (grafana, prometheus, milvus, .env.example)
  scripts/      #   entrypoint.sh and other deployment scripts
  ansible/      #   Ansible playbooks
  charts/       #   Helm charts (openrag-stack)
  cluster.yaml  #   Ray cluster config
scripts/        # Developer/operational CLI tools (check_layer_imports.py, data_indexer.py, postgres-init/)
tests/          # Integration tests (api_tests/, integration/)
docs/           # Documentation (Astro site + refactoring docs)
ui/             # Admin frontend (React SPA), served by the admin-ui container (infra/docker/ui.Dockerfile)
extern/         # Git submodules + compose service includes
```

Prompt templates ship inside the package at `openrag/prompts/templates/*.txt` and are
loaded into `DEFAULT_SEEDS` by `openrag/prompts/__init__.py`.

## Common Commands

### Development

```bash
# Install dependencies
uv sync

# Run the application locally (requires Docker services).
# The compose stack and its service configs live under infra/compose/.
cd infra/compose
docker compose up -d                 # GPU deployment
docker compose --profile cpu up -d   # CPU deployment

# Run with rebuild for development
docker compose up --build -d
```

### Testing

```bash
# Run all unit tests (fast, no infra needed)
uv run pytest tests/unit/

# Run a single test file
uv run pytest tests/unit/core/models/test_chunk.py

# Run tests matching a pattern
uv run pytest -k "test_chunk"

# Integration tests (need running services) / load tests
uv run pytest tests/integration/
uv run pytest tests/load/
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

The main application entry point is `openrag/api/main.py` which creates a FastAPI app with Ray initialization.

**Ray Actors** (distributed components):
- `Indexer` (`openrag/services/workers/indexer_pool.py`) - Handles document ingestion, chunking, and insertion into vector DB
- `TaskStateManager` (`openrag/services/workers/task_state.py`) - Tracks async task states: QUEUED → SERIALIZING → COMPLETED (or FAILED or CANCELLED)
- `Vectordb` / `MilvusDB` (`openrag/services/storage/milvus_store.py`) - Vector database operations with hybrid search (dense + BM25 sparse)
- `DocSerializer` (`openrag/services/workers/parsers/doc_serializer.py`) - Serializes files to Document objects using appropriate loaders
- `MarkerPool` / `MarkerWorker` (`openrag/services/workers/parsers/marker_workers.py`) - Pool of workers for PDF processing with Marker

**Pipeline Classes**:
- `RagPipeline` (`openrag/services/orchestrators/query_service.py`) - Orchestrates retrieval and LLM generation
- `RetrieverPipeline` (`openrag/core/retrieval/pipeline.py`) - Handles document retrieval and reranking
- `RAGMapReduce` (`openrag/services/orchestrators/query_service.py`) - Map-reduce for processing large document sets

### Document Processing Flow

1. Files uploaded via `POST /indexer/partition/{partition}/file/{file_id}` (multipart with `file=@…`)
2. `Indexer.add_file()` serializes file to Document using appropriate loader
3. Chunker splits document into chunks with contextual metadata
4. Embedder generates vectors via VLLM (OpenAI-compatible API)
5. Chunks inserted into Milvus with partition-based organization

### File Loaders (`openrag/services/workers/parsers/legacy_loaders/`)

Each file type has a dedicated loader that converts to markdown:
- `MarkerLoader` (default for PDF, in `pdf_loaders/marker.py`) - Supports OCR, complex layouts, tables
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

1. `format_context()` (`openrag/core/prompts/chat_prompt_builder.py`) numbers each source (`[Source 1]`, `[Source 2]`, ...) in the context and returns `(formatted_text, included_indices)` — the indices track which docs fit within the token budget
2. Prompt templates (`openrag/prompts/templates/*.txt`) instruct the LLM to append `[Sources: 1, 3, 5]` at the end of its response
3. `extract_and_strip_sources_block()` (`openrag/core/utils/source_filtering.py`) strips this tag from the response before sending to the client
4. `filter_sources_by_citations()` (`openrag/core/utils/source_filtering.py`) filters the source metadata to only include cited sources; if no `[Sources: ...]` tag is found at all, every presented source is kept instead (a missing tag means the model didn't report citations, not that it used none)
5. For streaming, the OpenAI router buffers the last 100 chars to catch the sources tag before it reaches the client

The `extra` field in API responses is a JSON string with these keys:

- `sources` — legacy field, kept as-is for existing clients (e.g. Twake): cited sources, or every presented source as a fallback when no `[Sources: ...]` tag was found.
- `presented_sources` — every source actually shown to the LLM (after `format_context()`/`format_web_context()` truncation), regardless of citation. Always present; a client can fall back to this ("sources consulted") when nothing was cited.
- `cited_sources` — strictly what the model cited via the tag; unlike `sources`, this never falls back to "everything" — it's `[]` whenever no tag was found. Chainlit is expected to move to this field, falling back to `presented_sources` in its UI when `cited_sources` is empty.
- `citations_reported` (bool) — `true` only when the model actually emitted a `[Sources: ...]` tag (even an empty/`none` one); `false` when the tag was missing entirely, which is the only case where `sources` falls back to keeping everything. Lets a client tell "the model cited every source" apart from "the model didn't report citations at all".
- `all_retrieved_sources` — the complete retrieval set, captured before the context-token-budget truncation, so it also includes documents/web results that didn't fit in the prompt (and, on the map-reduce path, the original retrieved docs rather than the LLM-generated summaries). Only included when the request sets `metadata.include_all_retrieved_sources: true` — it's debug/eval telemetry, gated off by default since retrieval is uncapped up to `retriever.top_k` while the context budget only fits a handful of documents.

### API Routers (`openrag/api/routers/`)

- `user/chat.py` - OpenAI-compatible `/v1/chat/completions` endpoint
- `admin/indexing.py` - Document ingestion endpoints
- `user/search.py` - Semantic search endpoints
- `admin/partitions.py` - Partition management (multi-tenant document collections)
- `admin/users.py` - User and membership management
- `admin/jobs.py` - Task queue monitoring
- `admin/workspaces.py` - Workspace CRUD and file management
- `admin/tools.py` - Tools like `extractText` at `/v1/tools/execute` (tool param requires JSON: `{"name": "extractText"}`)

### User Management & Authentication

The system uses token-based authentication with role-based access control (RBAC) for multi-tenant partition access.

**Database Schema** (PostgreSQL with SQLAlchemy, in `openrag/services/persistence/schema.py`):
- `users` - User accounts with `id`, `external_user_id`, `display_name`, `token` (SHA-256 hashed), `is_admin`, `file_quota`, `file_count`
- `files` - File records with `file_id`, `partition_name`, `file_metadata`, `created_by` (FK to users), `relationship_id`, `parent_id`
- `partition_memberships` - Join table linking users to partitions with roles (`owner`, `editor`, `viewer`)
- `partitions` - Document collections with cascade delete to files and memberships
- `workspaces` - Named file subsets within a partition for scoped search/chat
- `workspace_files` - Join table linking workspaces to files

**Authentication Flow** (`AuthMiddleware` from `openrag/api/middleware/auth.py`, registered in `openrag/api/main.py`):
1. Token extracted from `Authorization: Bearer <token>` header (or `?token=` query param for `/static` routes)
2. Token hashed with SHA-256, looked up in database
3. User info and accessible partitions set on `request.state.user` and `request.state.user_partitions`
4. Bypassed for: `/docs`, `/openapi.json`, `/redoc`, `/health_check`, `/version`, `/chainlit/*` — except in OIDC mode the three docs paths (`/docs`, `/redoc`, `/openapi.json`) are login-gated instead of public (see Middleware Behavior below)
5. If `AUTH_TOKEN` env var is not set, defaults to admin user (id=1) for all requests

**Role Hierarchy** (`openrag/services/orchestrators/auth_service.py`):
```python
ROLE_HIERARCHY = {"viewer": 1, "editor": 2, "owner": 3}
```

**Permission Dependencies** (`openrag/api/dependencies/auth.py`):
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

**Core Implementation** (`PartitionFileManager` in `openrag/services/persistence/partition_repo.py`):
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

**Configuration** (`conf/config.yaml` → `websearch:` block, env vars):
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
- `openrag/services/websearch/` — `WebSearchService` (`service.py`), `BaseWebSearchProvider` (`base.py`), `StaanProvider` (`providers/staan.py`)
- `openrag/core/prompts/chat_prompt_builder.py` — `format_web_context()` formats web results as numbered source blocks
- `openrag/services/orchestrators/query_service.py` — `_prepare_for_web_only()`, web search logic in `_prepare_for_chat_completion()`
- `openrag/api/routers/user/chat.py` — `__prepare_sources()` merges document and web sources

### Indexing Status Callbacks

Indexing is asynchronous, so a client can either poll the task-status URL or
hand OpenRag a URL to notify once the task settles.

**Request fields** (multipart form, on `POST` and `PUT /indexer/partition/{partition}/file/{file_id}`):
- `callback_url` — POSTed once the task reaches a terminal state
- `callback_token` — sent as `Authorization: Bearer <token>` on that POST; requires an `https`
  `callback_url` (both the router and the sender refuse a token over plain HTTP)

**Body:** `{"partition", "file_id", "status": "success"|"error", "timestamp", "metadata": {"file_rev", "datetime", "doctype"}}`.
The three metadata fields are echoed verbatim from the upload metadata — never recomputed from the
stored copy. `file_rev` is the file's CouchDB revision on the cozy side, used by the receiving
route for its staleness check, so a *stale* revision is dropped as outdated while an absent one is
accepted as current content. The three fields are a deliberate whitelist, not a pass-through:
`_build_metadata` also puts `source` (the server's on-disk path), `content_sha256` and `file_size`
in that dict, and the callback target is a caller-supplied URL.

**Guarantees:** one attempt, no retries, two independent 5 s deadlines (the DNS check, then the
request), never raises — a failed callback is logged and cannot change the indexing outcome. No
`callback_url` → strict no-op. A user-cancelled task sends nothing (only a real failure notifies
`"error"`), which is why the sender keys off the return value of `set_failed_if_not_cancelled` and
the pool's pre-flight handler catches `Exception`, not `BaseException`.

**Not guaranteed:** delivery. The send is awaited on the worker's slot, so a blackholing target costs
up to 10 s of indexing throughput per file, and an actor lost before the task settles notifies
nothing at all. Clients keep a timeout and fall back to polling the task-status URL.

**Rolling deploys:** the worker actors are named, detached and `get_if_exists`, so changing
`process_file`'s remote contract requires bumping `_INDEXER_ACTOR_PROTOCOL_VERSION` in
`indexer_pool.py` (v3 → v4 for `callback_url`/`callback_token`). Without the bump, new replicas
attach to the previous release's actors and every submit raises `TypeError`. Old generations are
retired with `services/workers/retire_indexer_generation.py`.

**SSRF guard:** `callback_url` is caller-supplied and fetched by the server, so it goes through
`is_safe_url()` plus a DNS check twice — in the router (immediate `400`) and again in the sender (a
direct caller bypasses the router). Both run in the router too: accepting a hostname the sender will
later refuse queues a job whose callback can never arrive. `INDEXING_CALLBACK_ALLOW_PRIVATE_URLS=true` /
`indexing_callback.allow_private_urls` lifts the *address* half of the guard for dev stacks whose
target is a local instance; the scheme check always applies. Keep it off in production: with it on,
any user allowed to upload can make the server POST to an internal address.

**Key files:**
- `openrag/services/workers/indexing_callback.py` — `send_indexing_callback()` (was `webhook.py`; the
  target is a normal authenticated route now, not an unauthenticated webhook trigger)
- `openrag/core/utils/url_safety.py` — `is_safe_url(url, *, allow_private_hosts=False)`, shared with
  the web-search content fetcher and the MCP `index_url` tool (both keep the strict default)
- `openrag/core/config/indexation.py` — `IndexingCallbackConfig`

Both fields travel the same chain as the rest of an indexing job: `api/routers/admin/indexing.py` →
`services/orchestrators/indexing_service.py` → `core/indexing/dispatcher.py` (port) →
`services/workers/dispatcher.py` → `indexer_pool.py` → `indexer_actor.py` → `indexing_callback.py`.

### File Quota System

Per-user file quota enforcement tracked via the `file_count` and `file_quota` columns on `users`, and `created_by` on `files`.

**How it works:**
- `files.created_by` records which user uploaded each file (nullable for pre-migration files)
- `users.file_count` is incremented/decremented in application code (in `PartitionFileManager`) — no SQL triggers
- Decrements use `func.greatest(file_count - N, 0)` to prevent negative values from race conditions
- `delete_partition` queries per-uploader counts before cascade delete, then bulk decrements
- Quota check (`check_user_file_quota` in `openrag/api/dependencies/auth.py`) runs on upload, considering both indexed files and pending tasks

**Quota logic (`file_quota` column):**
- `None` → use global default (`DEFAULT_FILE_QUOTA` env var, default `-1`)
- `< 0` → unlimited
- `>= 0` → specific limit
- Admins always bypass quota checks

**Key design decisions:**
- Counts are tracked per **uploader** (whoever calls the upload API), not per partition owner
- `created_by` uses `ondelete="SET NULL"` so deleting a user doesn't cascade-delete their files
- `Indexer.delete_file` and `MilvusDB.delete_file/delete_partition` don't need a `user_id` parameter — the uploader is looked up from `files.created_by`

**Migration:** `openrag/services/persistence/migrations/alembic/versions/c224d4befe71_add_file_count_and_file_quota.py`

### Alembic Migration Idempotency

`Base.metadata.create_all()` runs at app startup (`PartitionFileManager.__init__` in `openrag/services/persistence/partition_repo.py`), so a freshly bootstrapped database already contains the full current-model schema before alembic ever touches it. Migrations must therefore be **idempotent** — re-applying an `ADD COLUMN` / `CREATE TABLE` / `CREATE INDEX` against an already-existing object would raise `DuplicateColumn` / `DuplicateTable`.

Guard every schema-mutating op with an inspector-based existence check (`table_exists`, `column_exists`, `index_exists`, `fk_exists`), in both `upgrade()` and `downgrade()`. For migrations that convert a column type, also short-circuit if the column is already the target type.

### Configuration

Configuration is a single YAML file validated with Pydantic models:
- Main config: `conf/config.yaml`
- Loaded by `openrag/core/config/loader.py` (`load_config()` exposed from `openrag/core/config/__init__.py`)
- Pydantic config classes live in `openrag/core/config/` (`root.py`, `auth.py`, `chunking.py`, `retrieval.py`, `indexation.py`, `endpoints.py`, `mcp.py`, `infrastructure.py`, `base.py`)

Environment variables override config values (see `infra/compose/.env.example`).

### Testing Structure

All tests live in a separate `tests/` tree (zero test files inside the `openrag/` package):
- Unit tests: `tests/unit/**/test_*.py` (pytest, mirrors the package structure; no external services needed)
- Integration tests: `tests/integration/api/*.py` (HTTP endpoint tests, requires running server) and `tests/integration/repos/*.py` (repo/store tests)
- Robot Framework tests: `tests/integration/robot/api/*.robot`
- Load/benchmark tests: `tests/load/`
- Shared fixtures: `tests/unit/conftest.py` (mock ports), `tests/unit/api/conftest.py` (ASGI client), plus per-suite conftests
- Test config lives in `pyproject.toml` (`[tool.pytest.ini_options]`): `testpaths = ["tests"]`, `pythonpath = ["./openrag"]`, and the `env` block sets `PROMPTS_DIR=./openrag/prompts/templates` and `LOG_DIR`

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
from services.workers.ray_utils import call_ray_actor_with_timeout

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

All custom exceptions inherit from `OpenRAGError` (`openrag/core/utils/exceptions.py`):
- `VDBError` subclasses for vector database errors
- `EmbeddingError` for embedding failures

### Logging

Uses Loguru with structured logging:
```python
from core.utils.logging import get_logger
logger = get_logger()
logger.bind(file_id=file_id, partition=partition).info("Message")
```

### Import Conventions

Use absolute imports from the `openrag/` directory (which is the Python path root):
```python
# Correct - absolute imports
from services.workers.ray_utils import call_ray_actor_with_timeout
from core.utils.logging import get_logger
from core.config import load_config

# Avoid relative imports across packages
# from .ray_utils import ...  # Only within same package
```

### OIDC Authentication (OpenID Connect)

OpenRag supports two authentication modes, controlled by the `AUTH_MODE` environment variable:

**Token Mode** (`AUTH_MODE=token`, default):
- Bearer token authentication via `Authorization: Bearer <AUTH_TOKEN>` header
- Existing behavior unchanged
- Suitable for programmatic access, CI/CD, and testing
- Admin user (id=1) created with `AUTH_TOKEN` env var or random token on bootstrap

**OIDC Mode** (`AUTH_MODE=oidc`):
- OpenID Connect Authorization Code + PKCE flow
- Users authenticate via an external IdP (Keycloak, LemonLDAP::NG, etc.)
- Browser UI (Chainlit, Indexer) redirects to IdP login
- Opaque session tokens stored in `openrag_session` httpOnly cookie
- Bearer `users.token` still accepted for programmatic access

**Env Variables** (required when `AUTH_MODE=oidc`):

| Variable | Purpose | Example |
|----------|---------|---------|
| `OIDC_ENDPOINT` | Issuer URL for auto-discovery | `https://idp.example.com/realms/openrag` |
| `OIDC_CLIENT_ID` | Client registered at IdP | `openrag` |
| `OIDC_CLIENT_SECRET` | Client secret | (provided by IdP) |
| `OIDC_REDIRECT_URI` | Callback URL — the **front door** that serves the UI *and* reaches the backend `/auth/callback` (the admin-ui / proxy port, **not necessarily** `APP_PORT`); must match IdP config | `https://openrag.example.com/auth/callback` |
| `OIDC_TOKEN_ENCRYPTION_KEY` | Fernet key for token encryption | (generate via: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |

**Optional Env Variables**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OIDC_CLAIM_SOURCE` | `id_token` | Where to read claims for claim mapping: `id_token` (verified JWT) or `userinfo` (`/userinfo` endpoint) |
| `OIDC_CLAIM_MAPPING` | (none) | CSV of `db_field:claim` pairs to sync IdP claims into the users row on every login (whitelist: `display_name`, `email`). Unset = no post-login update. |
| `OIDC_SCOPES` | `openid email profile offline_access` | Space-separated scope list (include `offline_access` for refresh tokens) |
| `OIDC_POST_LOGOUT_REDIRECT_URI` | — | URL the IdP sends the user to after RP-initiated logout. No default (an OpenRag URL would re-trigger OIDC login) |
| `OIDC_AUTO_PROVISION_LOGIN` | `false` | When `true`, an unknown `sub` triggers on-the-fly creation of a non-admin user from the ID-token claims (`name`/`preferred_username` → `display_name`, `email` → `email`). Default keeps the strict admin-pre-provisioning policy below. |

**User Matching & Provisioning**:

When a user logs in via OIDC, matching is **exclusively** by `users.external_user_id == sub` (the stable OIDC claim). There is no email fallback. If the `sub` is unknown, the callback either:
- returns `403 "User not registered"` (default — admins must pre-create every user), or
- creates a non-admin user from the ID-token claims when `OIDC_AUTO_PROVISION_LOGIN=true`. Auto-provisioned users inherit the default file quota; `is_admin` is **always** `false` (operators can promote afterwards via `/users/{id}` or `/users/`).

Optionally, if `OIDC_CLAIM_MAPPING` is set, after a successful match the callback reads the configured claims (from the ID token or `/userinfo`, per `OIDC_CLAIM_SOURCE`) and updates the user row. The writable whitelist is strict — only `display_name` and `email` are allowed; `is_admin`, `external_user_id`, `file_quota`, `token` are never writable via claim mapping.

**Admin Pre-provisioning**: Admins create users with the `external_user_id` matching the IdP's `sub` claim for that user. Example:
```bash
curl -X POST http://localhost:8080/users/ \
  -H "Authorization: Bearer <AUTH_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Alice", "external_user_id": "kc-alice-uuid", "is_admin": false}'
```

**Database Schema**:

Columns on `users` table relevant to OIDC:
- `external_user_id` (String, unique, nullable): Must equal the IdP's `sub` for OIDC matching
- `email` (String, unique, nullable): Pure metadata; populated manually or via claim mapping. Not used for matching.

New table `oidc_sessions`:
- `session_token_hash` (unique): SHA-256 of the opaque session token
- `user_id` (FK): User this session belongs to
- `sid` (nullable): OIDC session identifier (used for back-channel logout)
- `sub` (required): OIDC `sub` claim (stable user identifier)
- `id_token_encrypted`, `access_token_encrypted`, `refresh_token_encrypted`: Fernet-encrypted IdP tokens
- `access_token_expires_at`, `session_expires_at`: Token expiry times
- `revoked_at` (nullable): Set on back-channel logout or manual revocation

**Auth Endpoints** (all bypass the normal middleware):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/auth/login` | Start Authorization Code + PKCE flow; redirects to IdP |
| GET | `/auth/callback` | IdP callback; creates session, sets cookie; redirects to `next_url` |
| POST | `/auth/backchannel-logout` | IdP-driven logout (OIDC spec); revokes sessions by `sid` |
| GET | `/auth/logout` | RP-initiated logout; invalidates session + redirects to IdP |
| GET | `/auth/me` | (debug) Returns current user and session expiry |

**Session Management**:

- Session token: URL-safe opaque token (`secrets.token_urlsafe(32)` — ~43 chars from 32 bytes of randomness), hashed (SHA-256) before storage
- Cookie: `openrag_session` (httpOnly, Secure if HTTPS, SameSite=Lax, Path=/, no Domain=)
- TTL: Aligned with `access_token_expires_at`; auto-refresh if `refresh_token` available (<60s before expiry)
- Revocation: Via back-channel logout or manual invalidation

**Middleware Behavior**:

- UI paths (`/`, `/chainlit`, `/static`) in OIDC mode without auth → 302 redirect to `/auth/login?next=...`
- Interactive docs (`/docs`, `/redoc`, `/openapi.json`): **public in token mode** (bypassed), but **login-gated in OIDC mode** — an unauthenticated browser is 302-redirected to `/auth/login`; a valid session renders them. This stops the full API surface + schema from being served anonymously in production. The set is `AuthBypassConfig.oidc_gated_paths` (default `("/docs", "/redoc", "/openapi.json")`); override it to `()` to keep docs public under OIDC.
- API paths (`/v1`, `/indexer`, `/search`, etc.) without auth:
  - **Token mode** → `403 {"detail": "Missing token"}` (no bearer) or `403 {"detail": "Invalid token"}` (unknown bearer). The 403 status is a legacy contract the robot suite asserts (`tests/api/`).
  - **OIDC mode** → `401 {"detail": "Unauthenticated"}` (no usable session/bearer and the path isn't a UI redirect target).
- Programmatic access: Bearer `users.token` accepted in both modes

**See Also**: Full configuration and troubleshooting guide at `docs/content/docs/documentation/oidc.md` (quick start: `docs/content/docs/documentation/sso-quickstart.md`).
