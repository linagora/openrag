# Roadmap: OpenRAG Codebase Hardening

## Overview

This roadmap systematically addresses reliability and security issues across the OpenRAG codebase. Starting with isolated quick fixes, we progressively harden exception handling across API, core services, and pipeline layers. Then we address async infrastructure issues before hardening scripts and cleaning up configuration tech debt. Every fix maintains existing external API behavior and passes all 93 existing tests.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Quick Security Fixes** - Isolated single-file bugs and security issues
- [ ] **Phase 2: Exception Handling - API Layer** - Replace broad exception handling in routers
- [ ] **Phase 3: Exception Handling - Core Services** - Replace broad exception handling in components and pipeline
- [ ] **Phase 4: Async Infrastructure** - Fix async file I/O and Ray actor patterns
- [ ] **Phase 5: Script & Health Hardening** - Improve restore script and health checks
- [ ] **Phase 6: Configuration Cleanup** - Clean up Hydra and legacy code

## Phase Details

### Phase 1: Quick Security Fixes
**Goal**: Fix isolated security issues and bugs that don't require architectural changes
**Depends on**: Nothing (first phase)
**Requirements**: BUG-01, SEC-01, SEC-03
**Success Criteria** (what must be TRUE):
  1. httpx client in app_front.py creates proper timeout object without nesting
  2. Database connection URLs are constructed using SQLAlchemy URL.create() instead of string concatenation
  3. File upload metadata is validated against Pydantic schema before processing
  4. All 93 existing tests continue passing
**Plans**: 3 plans

Plans:
- [ ] 01-01-PLAN.md — Fix nested httpx.Timeout bug in Chainlit frontend
- [ ] 01-02-PLAN.md — Replace unsafe PostgreSQL URL string interpolation with SQLAlchemy URL.create()
- [ ] 01-03-PLAN.md — Add Pydantic schema validation for file upload metadata

### Phase 2: Exception Handling - API Layer
**Goal**: Replace broad exception handling with specific exception types in all API routers
**Depends on**: Phase 1
**Requirements**: SEC-02 (routers subset)
**Success Criteria** (what must be TRUE):
  1. All router exception handlers catch specific exception types (OpenRAGError subclasses, Pydantic validation errors, Ray errors)
  2. Generic HTTP 500 responses include structured error details without exposing internals
  3. Streaming endpoints handle cancellation and timeout exceptions explicitly
  4. All 93 existing tests continue passing
**Plans**: 3 plans

Plans:
- [ ] 02-01-PLAN.md — Replace 8 exception handlers in OpenAI router (streaming and non-streaming endpoints)
- [ ] 02-02-PLAN.md — Replace 5 exception handlers in indexer router (file operations and task management)
- [ ] 02-03-PLAN.md — Replace 6 exception handlers in tools, utils, actors, and extract routers

### Phase 3: Exception Handling - Core Services
**Goal**: Replace broad exception handling with specific exception types in components and pipeline
**Depends on**: Phase 2
**Requirements**: SEC-02 (components and pipeline subset)
**Success Criteria** (what must be TRUE):
  1. Indexer, Vectordb, and loader components catch specific exceptions (VDBError, EmbeddingError, file I/O errors)
  2. Pipeline and retriever components distinguish between retrieval failures, LLM failures, and data errors
  3. Ray actor method exception propagation is explicit and typed
  4. All 93 existing tests continue passing
**Plans**: 4 plans

Plans:
- [ ] 03-01-PLAN.md — Replace 17 exception handlers in vectordb and metadata operations
- [ ] 03-02-PLAN.md — Replace 10 exception handlers in indexer, embeddings, and chunker
- [ ] 03-03-PLAN.md — Replace 19 exception handlers in document loaders
- [ ] 03-04-PLAN.md — Replace 5 exception handlers in pipeline and LLM components

### Phase 4: Async Infrastructure
**Goal**: Eliminate blocking I/O operations in async contexts
**Depends on**: Phase 3
**Requirements**: PERF-01, PERF-02
**Success Criteria** (what must be TRUE):
  1. All async file loaders use aiofiles or thread pool executor for file I/O operations
  2. Restore script uses async Ray actor calls instead of blocking ray.get()
  3. No blocking file operations occur in async request handlers
  4. All 93 existing tests continue passing
**Plans**: 2 plans

Plans:
- [ ] 04-01-PLAN.md — Convert BaseLoader.save_content to async and update callers, plus async restore script
- [ ] 04-02-PLAN.md — Convert blocking file I/O in 6 loaders to asyncio.to_thread

### Phase 5: Script & Health Hardening
**Goal**: Make restore script resilient to failures and improve health check observability
**Depends on**: Phase 4 (restore script async changes must be in place)
**Requirements**: DEBT-01, DEBT-02
**Success Criteria** (what must be TRUE):
  1. Health endpoint reports LLM and VLM service availability with response time metrics
  2. Restore script stops execution and rolls back on critical failures
  3. Restore script logs detailed progress with file counts and error summaries
  4. All 93 existing tests continue passing
**Plans**: TBD

Plans:
- [ ] 05-01: [TBD during planning]

### Phase 6: Configuration Cleanup
**Goal**: Remove technical debt from configuration and legacy compatibility code
**Depends on**: Phase 5
**Requirements**: DEBT-03, DEBT-04
**Success Criteria** (what must be TRUE):
  1. Hydra configuration version is set properly without suppressing warnings
  2. Legacy partition prefix backward compatibility is either removed or marked deprecated with migration timeline
  3. Configuration loading emits no warnings during application startup
  4. All 93 existing tests continue passing
**Plans**: TBD

Plans:
- [ ] 06-01: [TBD during planning]

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Quick Security Fixes | 0/3 | Not started | - |
| 2. Exception Handling - API Layer | 0/? | Not started | - |
| 3. Exception Handling - Core Services | 0/? | Not started | - |
| 4. Async Infrastructure | 0/2 | Not started | - |
| 5. Script & Health Hardening | 0/? | Not started | - |
| 6. Configuration Cleanup | 0/? | Not started | - |
