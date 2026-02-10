---
phase: 02-exception-handling-api-layer
plan: 02
subsystem: api
tags: [fastapi, exception-handling, ray, error-handling]

# Dependency graph
requires:
  - phase: 02-exception-handling-api-layer
    provides: Custom exception hierarchy from 02-01-PLAN.md
provides:
  - Tiered exception handling for file operations (OSError for disk I/O)
  - Tiered exception handling for Ray task operations (RayTaskError, ValueError)
  - User-friendly error messages without internal path/stack trace exposure
affects: [02-03, api-security, error-handling]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Tiered exception catching: HTTPException → specific types → generic Exception"
    - "Structured logging with context (file_id, partition, task_id)"
    - "Error message sanitization (no str(e) exposure)"

key-files:
  created: []
  modified:
    - openrag/routers/indexer.py

key-decisions:
  - "OSError is the base class for all disk I/O errors (IOError, PermissionError, FileNotFoundError)"
  - "Ray actor not-found errors manifest as ValueError, not RayTaskError"
  - "JSONDecodeError caught separately for malformed log file handling"

patterns-established:
  - "Always re-raise HTTPException first to preserve FastAPI error responses"
  - "Use logger.bind() for structured logging context instead of separate parameters"
  - "Generic Exception handlers log full traceback but return sanitized user message"

# Metrics
duration: 2min
completed: 2026-02-10
---

# Phase 02 Plan 02: Indexer Router Exception Handling Summary

**Replaced 5 bare exception handlers in indexer router with tiered handling for file operations and task management, preventing internal detail exposure**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-10T17:02:09Z
- **Completed:** 2026-02-10T17:04:16Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- File upload endpoints now catch OSError specifically for disk I/O failures before falling back to generic Exception
- Task management endpoints distinguish between RayTaskError (task execution failure), ValueError (actor not found), OSError (log file read), and JSONDecodeError (malformed logs)
- All generic error responses sanitized to prevent file path or internal stack trace exposure
- Structured logging with file_id, partition, and task_id context for debugging

## Task Commits

Each task was committed atomically:

1. **Tasks 1 & 2: Replace file upload and task management exception handlers** - `d0f1e2e` (fix)

Note: Both tasks modified the same file (openrag/routers/indexer.py), so changes were committed together after both tasks completed.

## Files Created/Modified
- `openrag/routers/indexer.py` - Added tiered exception handling for 5 endpoints: add_file, put_file, get_task_error, get_task_logs, cancel_task

## Decisions Made

**OSError covers all disk I/O errors:** Used OSError as the base class to catch IOError, PermissionError, FileNotFoundError, and disk full errors without needing separate handlers.

**Ray actor errors split by type:** RayTaskError for task execution failures, ValueError for "actor not found" errors (Ray's actual behavior).

**JSONDecodeError for log parsing:** Separate handler for malformed log file entries in get_task_logs to distinguish from I/O failures.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Indexer router exception handling complete
- Ready for plan 02-03 (remaining routers: openai, search, tools, partition, users, queue)
- Pattern established for tiered exception handling can be replicated across all routers

## Self-Check: PASSED

- FOUND: 02-02-SUMMARY.md
- FOUND: d0f1e2e

---
*Phase: 02-exception-handling-api-layer*
*Completed: 2026-02-10*
