# Refactoring OpenRAG for Enterprise‑Grade RAG

## The Problem

Building a production‑grade RAG system is far more complex than simply chaining an embedder, a vector database, and an LLM. 
At enterprise scale, you need a flexible, secure, observable, and highly configurable architecture that can adapt to different workloads, data types, and deployment environments. 
A real system must support:

- **Fine‑grained configuration per partition** — each knowledge partition can define its own indexing pipeline, retrieval strategy, chunking rules, metadata extraction, and ranking logic.
- **Multiple LLM and VLM endpoints** — fully swappable at runtime, enabling fallback, specialization (e.g., vision models for images), and cost‑based routing.
- **Multiple vector databases** — easily swappable; Milvus by default, with Qdrant and pgvector available on demand.
- **Distributed processing at scale** — Ray‑based parallelization for heavy workloads such as batch document parsing,
- **Observability and monitoring** — Prometheus metrics for every operation (indexing, embedding, retrieval, LLM calls) combined with Grafana dashboards for real‑time monitoring, alerting, and performance insights.
- **Authentication & security** — full support for OIDC authentication, enabling secure access control, multi‑tenant deployments, and integration with enterprise identity providers.
- **User management with fine‑grained access rights** — permissions at the level of collections/partitions, ensuring that users, teams, or tenants can only access the data and pipelines they are authorized to use.
- **Prompt management** — centralized management of system prompts, retrieval prompts, captioning prompts etc. and task‑specific prompts, with auditability, and per‑partition overrides.
- **Testability and reliability** — core logic must be testable locally without any external infrastructure, ensuring predictable behavior, easier CI/CD, and safer refactoring.

### How do we structure the code so that all of this is modular, decoupled, and maintainable?

## High Level Architecture

```jsx
┌─────────────────────────────────────────────────────────────────────┐
│                        API Layer (FastAPI)                          │
│  FastAPI routers, middleware, auth dependencies                     │
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

## Project Structure

```jsx
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
│   ├── middleware/                    #   Instrumentation, security headers
│   ├── routers/{admin,auth,user}/     #   Thin route handlers
│   └── schemas/{admin,auth,user}/     #   Request/response Pydantic models
│
├── prompts/templates/                 #   Prompt templates (seeded to DB on boot)
├── conf/                              #   YAML config files per environment
├── infra/                             #   Docker, Compose, Grafana, Prometheus, nginx
├── tests/                             #   Unit + integration + load tests
└── ui/                                #   React admin dashboard
```

## The 3-Layer Rule

### Layer 1: Core (zero infrastructure imports)

The `core` layer defines **what** the system does. The `services` layer defines **how**.

### Layer 2: Services (implements core interfaces)

### Layer 3: API (thin controllers)

The route reads the request, calls the service, maps the response. No business logic.

## See also

- [REFACTORING_STRATEGY_v1](./REFACTORING_STRATEGY_v1.md)
- [REFACTORING_DEV_WORKFLOW](./REFACTORING_DEV_WORKFLOW.md)