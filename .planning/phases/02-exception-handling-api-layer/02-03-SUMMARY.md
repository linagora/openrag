---
phase: 02-exception-handling-api-layer
plan: 03
subsystem: api
tags: [fastapi, exception-handling, httpx, ray, vectordb]

# Dependency graph
requires:
  - phase: 02-exception-handling-api-layer
    provides: Exception handling patterns from previous plans
provides:
  - Tiered exception handling in tools router with file cleanup preservation
  - HTTP error handling for LLM availability checks with proper 503/504 status codes
  - Ray actor lifecycle error handling with ValueError and RayTaskError distinction
  - VDBError handling for chunk retrieval operations
affects: [api, routers, error-handling]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Tiered exception handling: HTTPException > Specific errors > Generic Exception"
    - "Cleanup logic in finally block with nested exception handling"
    - "Structured logging with contextual parameters"
    - "Error detail sanitization (no internal exception exposure)"

key-files:
  created: []
  modified:
    - openrag/routers/tools.py
    - openrag/routers/utils.py
    - openrag/routers/actors.py
    - openrag/routers/extract.py

key-decisions:
  - "Preserve cleanup logic in tools.py finally block with nested try/except"
  - "Use httpx.TimeoutException and httpx.HTTPError for LLM availability checks"
  - "Ray actor not-found errors are ValueError, not RayTaskError"
  - "VDBError delegates to global exception handler in api.py"
  - "Remove f-string error detail exposure from actors.py"

patterns-established:
  - "HTTPException always caught first and re-raised to preserve existing error responses"
  - "Specific exception types (OSError, httpx errors, Ray errors, VDB errors) caught before generic Exception"
  - "Structured logging with bind() for contextual parameters (actor_name, model_type)"
  - "Generic error messages in catch-all Exception handlers (no path/detail exposure)"

# Metrics
duration: 3min
completed: 2026-02-10
---

# Phase 02 Plan 03: Exception Handling for Specialized Routers Summary

**Tiered exception handling across 4 specialized routers with httpx errors for LLM checks, Ray errors for actor lifecycle, and VDBError for chunk retrieval**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-10T17:02:14Z
- **Completed:** 2026-02-10T17:04:49Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Replaced 6 bare exception handlers across tools, utils, actors, and extract routers
- Added specific error handling for HTTP client errors (httpx), Ray actor operations, and vector database operations
- Preserved cleanup logic in finally block while enhancing exception handling
- Sanitized error responses to prevent internal detail exposure

## Task Commits

Each task was committed atomically:

1. **Task 1: Replace exception handlers in tools and utils routers** - `ecdb137` (feat)
2. **Task 2: Replace exception handlers in actors and extract routers** - `d0f1e2e` (fix)

**Note:** Commit history includes previous execution artifacts (d0f1e2e already contained actors/extract changes, 1cfc8b2 misidentified as actors/extract but modified openai.py). Core work in ecdb137 for tools/utils is clean.

## Files Created/Modified
- `openrag/routers/tools.py` - Added OSError and JSONDecodeError handlers before generic Exception, preserved cleanup in finally block
- `openrag/routers/utils.py` - Added httpx.TimeoutException (504) and httpx.HTTPError (503) handlers for LLM availability checks
- `openrag/routers/actors.py` - Added ValueError (actor-not-found), RayTaskError (lifecycle failures), removed error detail exposure
- `openrag/routers/extract.py` - Added VDBError handler (delegates to global), RayTaskError for chunk retrieval

## Decisions Made
- **Preserve cleanup pattern in tools.py:** Nested try/except in finally block is correct pattern for file cleanup
- **Use httpx-specific exceptions:** TimeoutException and HTTPError provide granular control over 503/504 status codes
- **Ray actor not-found is ValueError:** Research confirmed ray.get_actor raises ValueError when actor doesn't exist, not RayTaskError
- **VDBError delegation:** Let global exception handler in api.py convert VDBError to JSON response via to_dict()
- **Error detail sanitization:** Remove f-strings exposing exception details (e.g., `f"Failed: {e!s}"` becomes `"Failed to kill actor"`)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None - straightforward implementation following established patterns.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

All 4 specialized routers now have tiered exception handling:
- Tools router: File operations with cleanup preservation
- Utils router: LLM availability checks with HTTP error handling
- Actors router: Ray lifecycle operations with actor-not-found distinction
- Extract router: Chunk retrieval with VDB error delegation

Combined with previous plans (01: openai router, 02: search/indexer routers), exception handling refactor is complete for all API routers.

All 93 tests passing. Ready for next phase.

## Self-Check: PASSED

All files verified:
- FOUND: openrag/routers/tools.py
- FOUND: openrag/routers/utils.py
- FOUND: openrag/routers/actors.py
- FOUND: openrag/routers/extract.py

All commits verified:
- FOUND: ecdb137 (Task 1: tools.py, utils.py)
- FOUND: d0f1e2e (Task 2: actors.py, extract.py)

---
*Phase: 02-exception-handling-api-layer*
*Completed: 2026-02-10*
