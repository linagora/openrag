# Requirements: OpenRAG Codebase Hardening

**Defined:** 2026-02-10
**Core Value:** Improve codebase reliability and security by eliminating known bugs, replacing broad exception handling, hardening SQL construction, and fixing performance bottlenecks — without changing external behavior.

## v1 Requirements

Requirements for this hardening pass. Each maps to roadmap phases.

### Bug Fixes

- [ ] **BUG-01**: Nested `httpx.Timeout(timeout=httpx.Timeout(...))` in `app_front.py` is flattened to `httpx.Timeout(4 * 60.0)`

### Security

- [ ] **SEC-01**: Database connection URLs use `SQLAlchemy URL.create()` instead of string interpolation
- [ ] **SEC-02**: All bare `except Exception` handlers are replaced with specific exception types across the codebase (~40+ instances)
- [ ] **SEC-03**: File upload metadata is validated against a Pydantic schema before processing

### Tech Debt

- [ ] **DEBT-01**: Health check endpoint reports LLM and VLM service availability
- [ ] **DEBT-02**: Restore script stops execution and rolls back on critical failure
- [ ] **DEBT-03**: Hydra configuration version is properly set without warning suppression
- [ ] **DEBT-04**: Legacy partition prefix backward compatibility workaround is removed or marked deprecated with timeline

### Performance

- [ ] **PERF-01**: Restore script uses async Ray actor calls instead of blocking `ray.get()`
- [ ] **PERF-02**: Async loaders use non-blocking file I/O (aiofiles or thread pool executor)

## v2 Requirements

Deferred to future milestone. Tracked but not in current roadmap.

### Infrastructure

- **INFRA-01**: Rate limiting on API endpoints
- **INFRA-02**: Distributed tracing with trace IDs across async boundaries
- **INFRA-03**: Circuit breaking for LLM, VLM, and Milvus external services
- **INFRA-04**: Global request-level timeout management

### Testing

- **TEST-01**: Streaming response error scenario tests
- **TEST-02**: Ray actor timeout and cancellation tests
- **TEST-03**: Partition access control combination tests
- **TEST-04**: Database schema constraint violation tests
- **TEST-05**: Chunker edge case tests
- **TEST-06**: Loader error recovery tests

## Out of Scope

| Feature | Reason |
|---------|--------|
| Rate limiting | New infrastructure feature, not a fix |
| Distributed tracing | New infrastructure, separate milestone |
| Circuit breaking | New feature requiring architectural changes |
| Request-level timeouts | New feature, not fixing existing behavior |
| Test coverage expansion | Separate testing milestone |
| Scaling improvements | Architectural changes beyond code fixes |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| BUG-01 | Phase 1 | Pending |
| SEC-01 | Phase 1 | Pending |
| SEC-03 | Phase 1 | Pending |
| SEC-02 (routers) | Phase 2 | Pending |
| SEC-02 (components, pipeline) | Phase 3 | Pending |
| PERF-01 | Phase 4 | Pending |
| PERF-02 | Phase 4 | Pending |
| DEBT-01 | Phase 5 | Pending |
| DEBT-02 | Phase 5 | Pending |
| DEBT-03 | Phase 6 | Pending |
| DEBT-04 | Phase 6 | Pending |

**Coverage:**
- v1 requirements: 10 total
- Mapped to phases: 10
- Unmapped: 0 ✓

**Note:** SEC-02 is split across Phase 2 (API layer/routers) and Phase 3 (core services/components/pipeline) due to the large scope (~40+ instances).

---
*Requirements defined: 2026-02-10*
*Last updated: 2026-02-10 after roadmap creation*
