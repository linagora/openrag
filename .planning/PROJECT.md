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

<!-- v1.0 Milestone: Codebase Hardening (completed 2026-02-11) -->

- ✓ httpx.Timeout bug fixed in app_front.py — v1.0
- ✓ All ~40+ bare `except Exception` handlers replaced with specific exception types — v1.0
- ✓ SQL injection risk fixed — SQLAlchemy URL.create() for DB connection strings — v1.0
- ✓ Pydantic validation schema for file upload metadata — v1.0
- ✓ LLM/VLM health check reporting in health endpoint — v1.0
- ✓ Restore script stops execution and rolls back on critical failures — v1.0
- ✓ Blocking ray.get() replaced with async calls in restore script — v1.0
- ✓ Sync file I/O in async loaders fixed (asyncio.to_thread) — v1.0
- ✓ Hydra version warning suppression addressed properly — v1.0
- ✓ Legacy partition prefix backward compat marked deprecated — v1.0

### Active

<!-- No active requirements — milestone complete -->

None — v1.0 milestone complete. See v2 requirements in archived REQUIREMENTS.md.

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
- Milestone v1.0 archived at `.planning/milestones/`
- Test suite: 98 unit tests, all passing (grew from 93 during v1.0)
- Linting: ruff configured with line length 120, Python 3.12 target
- Changes must not break existing tests or external API contracts

## Constraints

- **Behavior preservation**: All fixes must maintain existing external API behavior — no breaking changes
- **Test suite**: All 98 existing tests must continue passing after each change
- **Linting**: All changes must pass `ruff check openrag/`
- **No new dependencies**: Prefer using existing libraries (aiofiles is acceptable if needed for async I/O)
- **Incremental**: Each fix should be independently committable and reviewable

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Fix all 40+ broad exception handlers | User wants comprehensive cleanup, not partial | Complete — all handlers replaced across 6 phases |
| Skip new features (rate limiting, tracing, circuit breaking) | Focus on fixing what exists, not adding new infrastructure | Enforced — deferred to v2 |
| Use SQLAlchemy URL.create() for DB URLs | Standard, safe approach recommended in CONCERNS.md | Complete — Phase 1 |
| Use asyncio.to_thread for blocking I/O | Matches existing VideoAudioLoader pattern, no new deps | Complete — Phase 4 |
| Tiered exception handling pattern | HTTPException re-raise → specific exceptions → generic fallback | Complete — Phases 2-3 |
| VDB-first rollback order | Prevents orphaned vectors (worse than orphaned RDB entries) | Complete — Phase 5 |
| Hydra version_base=None | Forward-compatible, no warning suppression | Complete — Phase 6 |
| Python stdlib DeprecationWarning for legacy prefix | Standard mechanism, no new dependencies | Complete — Phase 6 |

## Milestones

| Version | Name | Status | Date |
|---------|------|--------|------|
| v1.0 | Codebase Hardening | Complete | 2026-02-11 |

---
*Last updated: 2026-02-11 after v1.0 milestone completion*
