# External Integrations

**Analysis Date:** 2026-02-10

## APIs & External Services

**LLM (Large Language Model):**
- OpenAI-compatible API
  - Configuration: `BASE_URL`, `API_KEY`, `MODEL` env vars
  - Hydra config: `.hydra_config/config.yaml` -> `llm` section
  - Client: OpenAI SDK via `openrag/components/llm.py`
  - Usage: Chat completions, streaming responses via `openrag/routers/openai.py`
  - Timeout: 4 minutes for chat, 40 seconds for completions
  - Features: Streaming (SSE), non-streaming modes, retry logic (max_retries: 2)

**Visual Language Model (VLM):**
- OpenAI-compatible API (can be same as LLM)
  - Configuration: `VLM_BASE_URL`, `VLM_API_KEY`, `VLM_MODEL` env vars
  - Hydra config: `.hydra_config/config.yaml` -> `vlm` section
  - Usage: Image captioning in loaders (PDF, DOCX, PPTX, image files)
  - Controlled by: `IMAGE_CAPTIONING` env var (true/false)
  - Methods: `openrag/components/indexer/loaders/base.py` provides `get_image_description()`, `caption_images()`

**Embeddings Service (VLLM-compatible):**
- OpenAI-compatible embeddings API
  - Model: Jina Embeddings V3 (default, `jinaai/jina-embeddings-v3`)
  - Configuration: `EMBEDDER_MODEL_NAME`, `EMBEDDER_BASE_URL`, `EMBEDDER_API_KEY` env vars
  - Default URL: `http://vllm:8000/v1`
  - Base URL: configurable, defaults to internal VLLM service
  - Client: `OpenAI` SDK in `openrag/components/indexer/embeddings/openai.py`
  - Extra params: `truncate_prompt_tokens` (configurable max_model_len)

**Reranker Service:**
- HTTP REST API (Infinity-compatible)
  - Provider: Alibaba-NLP/gte-multilingual-reranker-base (default)
  - Configuration: `RERANKER_MODEL`, `RERANKER_BASE_URL`, `RERANKER_PORT` env vars
  - Default URL: `http://reranker:7997`
  - Client: `infinity-client` 0.0.76+ in `openrag/components/reranker.py`
  - Control: Enabled/disabled via `RERANKER_ENABLED` env var
  - Top-K results: Configurable via `RERANKER_TOP_K` (default 10)
  - Usage: Post-retrieval reranking of chunks for better relevance

**Transcription Service (Audio/Video):**
- OpenAI Whisper-compatible API
  - Configuration: `TRANSCRIBER_BASE_URL`, `TRANSCRIBER_API_KEY`, `TRANSCRIBER_MODEL` env vars
  - Default model: `openai/whisper-large-v3-turbo`
  - Default URL: `http://transcriber:8000/v1`
  - Used by: `VideoAudioLoader` for MP3, WAV, MP4, OGG, FLV, WMA, AAC files
  - Parameters: `TRANSCRIBER_MAX_CHUNK_MS`, `TRANSCRIBER_SILENCE_THRESH_DB`, `TRANSCRIBER_MIN_SILENCE_LEN_MS`

**OCR Service (Document Text Extraction):**
- OpenAI-compatible API for OCR/text extraction
  - Configuration: `OPENAI_LOADER_BASE_URL`, `OPENAI_LOADER_API_KEY`, `OPENAI_LOADER_MODEL` env vars
  - Default model: `dotsocr-model`
  - Default URL: `http://openai:8000/v1`
  - Used by: Document loaders for complex text extraction
  - Parameters: `OPENAI_LOADER_TEMPERATURE`, `OPENAI_LOADER_TIMEOUT`, `OPENAI_LOADER_MAX_RETRIES`, `OPENAI_LOADER_TOP_P`

**HuggingFace Model Hub:**
- Integration via transformers/LangChain
  - Token: `HUGGING_FACE_HUB_TOKEN` env var for authenticated downloads
  - Cache location: `~/.cache/huggingface` (mounted in Docker)
  - Used for: Loading reranker models, embedder models, language detection models
  - LangChain integration: `langchain-huggingface` package

**Indexer UI Backend:**
- HTTP REST API
  - Frontend communicates with FastAPI backend
  - Configuration: `API_BASE_URL`, `INCLUDE_CREDENTIALS` env vars
  - URL format: `http://X.X.X.X:APP_PORT` (port typically 8080)
  - Auth: HTTPBearer token support if `AUTH_TOKEN` is set
  - Endpoints consumed: `/indexer/*`, `/partition/*`, `/users/*`

## Data Storage

**Databases:**
- **Milvus Vector Database**
  - Type: Distributed vector database
  - Connection: `milvus` service (default) or configurable via `VDB_HOST`, `VDB_iPORT`
  - Client: `pymilvus` package with async support (`AsyncMilvusClient`)
  - Default port: 19530
  - Features: Hybrid search (dense vectors + BM25 sparse), partitioning, filtering
  - Collections: Named per deployment via `VDB_COLLECTION_NAME` env var (default: `vdb_test`)
  - Docker compose: `vdb/milvus.yaml` included as service

- **PostgreSQL 15 (Relational Database)**
  - Connection string: `postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}`
  - Default credentials: user=`root`, password=`root_password`, host=`rdb`, port=5432
  - ORM: SQLAlchemy with async support via `asyncpg`
  - Database names: `partitions_for_collection_{collection_name}` (per partition)
  - Tables: `files`, `partitions`, `users`, `partition_membership`
  - Migrations: Alembic in `openrag/scripts/migrations/`
  - Schema: `openrag/components/indexer/vectordb/utils.py` (File, Partition, User, PartitionMembership models)
  - Docker compose: `rdb` service with volume mounting

**File Storage:**
- Local filesystem only
  - Data directory: `./data` (configurable via `DATA_VOLUME` env var)
  - Model weights: `~/.cache/huggingface` (HuggingFace cache)
  - Log directory: `./logs` (configurable via `LOG_DIR` env var)
  - Config directory: `./.hydra_config` (configurable via `CONFIG_VOLUME` env var)
  - No S3, GCS, or cloud storage integration detected

**Caching:**
- Ray object store (in-process or distributed)
  - Used for task results and intermediate data
  - Memory-based with optional spilling to disk
- Python in-process caching via variables and dictionaries
- No Redis, Memcached, or external cache service

## Authentication & Identity

**Auth Provider:**
- Custom token-based authentication (no external OAuth/OIDC detected)
  - Bearer token: `AUTH_TOKEN` env var (optional, HTTPBearer scheme)
  - User management: In-database user and membership tables
  - Token storage: In PostgreSQL `users` table

**User Management:**
- HTTP endpoints: `openrag/routers/users.py`
  - `/users/` - List users (admin only)
  - `/users/` - Create new user (admin only)
  - `/users/{user_id}` - Get user details (admin only)
  - `/users/{user_id}` - Delete user (admin only)
  - `/users/{user_id}/regenerate_token` - Regenerate token (admin or self)
  - `/users/info` - Get current authenticated user

**Chainlit Authentication:**
- Password-based auth callback in `openrag/app_front.py`
  - Callback: `@cl.password_auth_callback`
  - Optional: Controlled by `CHAINLIT_AUTH_SECRET` env var
  - Persisted user state via Chainlit data layer (if enabled)

**Multi-tenancy:**
- Partition-based isolation
  - Users mapped to partitions via `PartitionMembership` table
  - Endpoints: `openrag/routers/partition.py` for partition management
  - Admin users have access to all partitions

## Monitoring & Observability

**Error Tracking:**
- No external error tracking detected (Sentry, DataDog, etc.)
- Application errors logged to stdout via Loguru

**Logs:**
- Loguru 0.7.3+ for structured logging
  - Log level: Configurable via `LOG_LEVEL` env var (DEBUG by default)
  - Output: stdout + file logging (configurable via `LOG_DIR` env var)
  - Context binding: Via `logger.bind(key=value)` for request correlation
  - No external log aggregation (ELK, Splunk, CloudWatch) detected

**Ray Logging:**
- Ray Dashboard: Available on port 8265 (disable in cluster mode)
  - Ray-specific logs: Controlled by `RAY_DEDUP_LOGS`, `RAY_ENABLE_RECORD_ACTOR_TASK_LOGGING`
  - Task-level logging for Ray actors (configurable)

**Metrics:**
- No Prometheus, StatsD, or similar metrics collection detected
- Memory monitoring: Ray's internal memory monitor (configurable via `RAY_memory_monitor_refresh_ms`)

## CI/CD & Deployment

**Hosting:**
- Docker containers (primary)
  - GPU profile: `docker compose up -d` (default with NVIDIA runtime)
  - CPU profile: `docker compose --profile cpu up -d`

- Kubernetes (optional)
  - Helm chart: `charts/openrag-stack/Chart.lock` present
  - Ray cluster capable of distributed deployment

**CI Pipeline:**
- GitHub Actions (`.github/workflows/` directory)
  - API tests: `.github/workflows/api_tests.yml`
  - Mock VLLM for CI: `.github/workflows/api_tests/mock_vllm.py`
  - No external CI service integration detected beyond GitHub Actions

**Deployment Options:**
- Docker Compose (single-machine or Compose v2 stack)
- Kubernetes via Helm charts
- Ray Serve (optional, controlled by `ENABLE_RAY_SERVE` env var)

## Environment Configuration

**Required env vars (LLM APIs):**
- `BASE_URL` - LLM API base URL
- `API_KEY` - LLM API key
- `MODEL` - LLM model name
- `VLM_BASE_URL` - Visual LLM API base URL
- `VLM_API_KEY` - Visual LLM API key
- `VLM_MODEL` - Visual LLM model name

**Required env vars (Embeddings):**
- `EMBEDDER_MODEL_NAME` - Embedder model (default: jinaai/jina-embeddings-v3)
- `EMBEDDER_BASE_URL` - Embedder API URL (default: http://vllm:8000/v1)
- `EMBEDDER_API_KEY` - Embedder API key (default: EMPTY)

**Required env vars (Database):**
- `POSTGRES_HOST` - PostgreSQL host (default: rdb)
- `POSTGRES_PORT` - PostgreSQL port (default: 5432)
- `POSTGRES_USER` - PostgreSQL user (default: root)
- `POSTGRES_PASSWORD` - PostgreSQL password (default: root_password)
- `VDB_HOST` - Milvus host (default: milvus)
- `VDB_iPORT` - Milvus port (default: 19530)
- `VDB_COLLECTION_NAME` - Milvus collection name (default: vdb_test)

**Optional env vars:**
- `AUTH_TOKEN` - Bearer token for API authentication
- `RERANKER_ENABLED` - Enable/disable reranker (default: true)
- `RERANKER_MODEL` - Reranker model name
- `IMAGE_CAPTIONING` - Enable image captioning (default: true)
- `LOG_LEVEL` - Logging level (default: DEBUG)
- `WITH_CHAINLIT_UI` - Mount Chainlit UI (default: true)

**Secrets location:**
- Environment variables (via `.env` or container secrets)
- PostgreSQL credentials in connection strings
- API keys in bearer tokens
- No dedicated secrets management detected (no Vault, AWS Secrets Manager, etc.)

## Webhooks & Callbacks

**Incoming:**
- None detected in codebase
- Application provides HTTP REST API endpoints only (no webhook receiver pattern)

**Outgoing:**
- None detected
- Application does not push events to external webhooks
- External service calls are pull-based (API requests to LLM, embeddings, reranker services)

**Async Task Callbacks:**
- Ray actor task status updates via polling (`TaskStateManager`)
- WebSocket support potentially via Chainlit (not explicitly in core code)
- Task queue monitoring endpoint: `openrag/routers/queue.py`
  - Task states: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED (or FAILED)

---

*Integration audit: 2026-02-10*
