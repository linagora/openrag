# OpenRAG — Codebase Hardening

## What This Is

OpenRAG is a modular Retrieval-Augmented Generation framework built with FastAPI, Ray, and Milvus. It provides document ingestion (PDF, DOCX, images, audio), hybrid vector search, and an OpenAI-compatible chat completions API with multi-tenant partition isolation. This project focuses on fixing bugs, security issues, tech debt, and performance problems identified during codebase analysis.

## Core Value

Improve codebase reliability and security by eliminating known bugs, replacing broad exception handling with specific error types, hardening SQL construction, and fixing performance bottlenecks — without changing external behavior.

## Requirements

### Validated

<!-- Existing capabilities confirmed from codebase analysis -->

- ✓ Document ingestion pipeline (upload, serialize, chunk, embed, insert) — existing
- ✓ Multi-format file loaders (PDF, DOCX, PPTX, images, audio, markdown, HTML, email) — existing
- ✓ Hybrid search (dense embeddings + BM25 sparse with RRF ranking) — existing
- ✓ OpenAI-compatible chat completions API with streaming — existing
- ✓ Multi-tenant partition system with user roles and access control — existing
- ✓ RAG pipeline with SingleQuery, MultiQuery, and HyDE retrieval strategies — existing
- ✓ Map-reduce summarization for large document sets — existing
- ✓ Domain-based file filtering in search — existing
- ✓ Task state tracking (QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED/FAILED) — existing
- ✓ Async distributed processing via Ray actors — existing
- ✓ Hydra-based configuration with environment variable overrides — existing
- ✓ Structured logging with Loguru — existing

### Active

<!-- Fixes identified from codebase concerns audit -->

- [ ] Fix nested httpx.Timeout bug in app_front.py
- [ ] Replace all ~40+ bare `except Exception` handlers with specific exception types
- [ ] Fix SQL injection risk — use SQLAlchemy URL.create() for DB connection strings
- [ ] Add Pydantic validation schema for file upload metadata
- [ ] Implement LLM/VLM health check reporting in health endpoint
- [ ] Fix restore script to stop execution on critical failures
- [ ] Replace blocking ray.get() in restore script with async calls
- [ ] Fix sync file I/O in async loaders (use aiofiles or thread pool)
- [ ] Address Hydra version warning suppression properly
- [ ] Remove or deprecate legacy partition prefix backward compat workaround

### Out of Scope

- Rate limiting — new feature, not a fix
- Distributed tracing — new infrastructure, separate milestone
- Circuit breaking for external services — new feature
- Request-level timeout management — new feature
- Test coverage gaps — separate testing milestone
- Scaling limits (collection loading, semaphore limits) — architectural changes

## Context

- Codebase map available at `.planning/codebase/` (architecture, stack, concerns, conventions, testing)
- All issues sourced from `.planning/codebase/CONCERNS.md` dated 2026-02-10
- Existing test suite: 93 unit tests, all passing
- Linting: ruff configured with line length 120, Python 3.12 target
- Changes must not break existing tests or external API contracts

## Constraints

- **Behavior preservation**: All fixes must maintain existing external API behavior — no breaking changes
- **Test suite**: All 93 existing tests must continue passing after each change
- **Linting**: All changes must pass `ruff check openrag/`
- **No new dependencies**: Prefer using existing libraries (aiofiles is acceptable if needed for async I/O)
- **Incremental**: Each fix should be independently committable and reviewable

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Fix all 40+ broad exception handlers | User wants comprehensive cleanup, not partial | — Pending |
| Skip new features (rate limiting, tracing, circuit breaking) | Focus on fixing what exists, not adding new infrastructure | — Pending |
| Use SQLAlchemy URL.create() for DB URLs | Standard, safe approach recommended in CONCERNS.md | — Pending |

---
*Last updated: 2026-02-10 after initialization*
