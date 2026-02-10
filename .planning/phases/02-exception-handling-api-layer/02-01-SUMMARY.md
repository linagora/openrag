---
phase: 02-exception-handling-api-layer
plan: 01
subsystem: api-layer
tags: [exception-handling, error-handling, openai-api, streaming]
dependency_graph:
  requires: [utils/exceptions/base.py, utils/exceptions/vectordb.py, components/ray_utils.py]
  provides: [tiered-exception-handling-openai-router]
  affects: [/v1/chat/completions, /v1/completions]
tech_stack:
  added: []
  patterns: [tiered-exception-handling, asyncio-cancellation-detection]
key_files:
  created: []
  modified: [openrag/routers/openai.py]
decisions:
  - Catch asyncio.CancelledError FIRST in streaming to detect client disconnection
  - Re-raise HTTPException unchanged to preserve status codes
  - Use generic error messages for Exception catch-all to prevent internal detail exposure
  - Log all exceptions with structured logging (code, error message)
metrics:
  duration_seconds: 149
  tasks_completed: 2
  files_modified: 1
  completed_date: 2026-02-10
---

# Phase 02 Plan 01: OpenAI Router Exception Handling Summary

**One-liner:** Replaced 7 bare exception handlers in OpenAI-compatible router with tiered exception handling for chat completions and completions endpoints

## What Was Accomplished

### Work Already Completed

This plan's work was **already completed** in commit `1cfc8b2` (mislabeled as "feat(02-03)"). The commit correctly implemented all required exception handling improvements for openai.py.

### Tasks Completed

**Task 1: Chat completions endpoint exception handling**
- Replaced 4 bare exception handlers with tiered handling:
  - Lines 167-175: get_partition_name error - catch HTTPException, VDBPartitionNotFound, then Exception
  - Lines 177-195: ragpipe.chat_completion - catch HTTPException, OpenRAGError, RayTaskError/TaskCancelledError, then Exception
  - Lines 206-253: streaming generator - catch asyncio.CancelledError FIRST, then RayTaskError/TaskCancelledError, OpenRAGError, Exception
  - Lines 256-270: non-streaming response - catch HTTPException, OpenRAGError, then Exception
- Status: ✅ Complete (commit 1cfc8b2)

**Task 2: Completions endpoint exception handling**
- Replaced 3 bare exception handlers with tiered handling:
  - Lines 308-326: get_partition_name - same pattern as chat completions
  - Lines 328-346: ragpipe.completions - catch HTTPException, OpenRAGError, RayTaskError/TaskCancelledError, Exception
  - Lines 355-369: non-streaming response - catch HTTPException, OpenRAGError, Exception
- Status: ✅ Complete (commit 1cfc8b2)

### Implementation Details

**Exception Handling Pattern:**
```python
# Pattern applied throughout
try:
    # operation
except HTTPException:
    raise  # Preserve status codes
except SpecificError as e:
    log.warning("...", code=e.code, error=e.message)
    raise HTTPException(status_code=e.status_code, detail=e.message)
except (RayTaskError, TaskCancelledError) as e:
    log.exception("...", error=str(e))
    raise HTTPException(status_code=500, detail="Generic message")
except Exception as e:
    log.exception("...", error=str(e))
    raise HTTPException(status_code=500, detail="Generic sanitized message")
```

**Streaming-Specific Pattern:**
```python
async def stream_response():
    try:
        # streaming logic
    except asyncio.CancelledError:
        log.info("Client disconnected during streaming")
        return  # Clean exit
    except (RayTaskError, TaskCancelledError) as e:
        yield SSE error event with code "RAY_TASK_ERROR"
    except OpenRAGError as e:
        yield SSE error event with e.code and e.message
    except Exception as e:
        yield SSE error event with generic message
```

**Key Improvements:**
1. **Client disconnection detection**: asyncio.CancelledError caught first in streaming to cleanly handle client disconnect
2. **Status code preservation**: HTTPException re-raised unchanged
3. **Security**: Generic error messages prevent internal detail exposure (no `str(e)` or f-strings with exception data)
4. **Observability**: Structured logging with error context (code, message)
5. **Ray task handling**: RayTaskError and TaskCancelledError caught specifically

## Verification Results

All verification checks passed:

1. ✅ **No bare exception handlers**: `grep -n "except Exception:" openrag/routers/openai.py` returns no matches
2. ✅ **Streaming error order correct**: asyncio.CancelledError appears first in stream_response
3. ✅ **HTTPException preserved**: All `except HTTPException` followed by `raise`
4. ✅ **No internal details exposed**: No `detail=str(e)` or `detail=f...{e}` patterns found
5. ✅ **All tests pass**: 93/93 tests passing
6. ✅ **No linting errors**: `ruff check` passes

## Deviations from Plan

### Pre-existing Work

**Work already completed in commit 1cfc8b2**
- **Found during:** Execution start
- **Issue:** Plan 02-01 work was already implemented in a previous commit labeled "feat(02-03)"
- **Resolution:** Verified existing implementation matches plan requirements exactly
- **Outcome:** No additional changes needed, work confirmed complete

This is not a deviation from the plan requirements - the plan was executed correctly, just in a prior session.

## Technical Notes

**Exception Types Imported:**
- `asyncio.CancelledError` - for streaming client disconnection
- `RayTaskError, TaskCancelledError` from `ray.exceptions` - for Ray actor failures
- `OpenRAGError` from `utils.exceptions.base` - base exception type
- `VDBPartitionNotFound` from `utils.exceptions.vectordb` - specific partition errors

**Endpoints Modified:**
- `POST /v1/chat/completions` - 4 exception handlers replaced
- `POST /v1/completions` - 3 exception handlers replaced

**Error Flow:**
1. HTTPException → re-raised immediately (preserves status codes)
2. Domain-specific errors (VDBPartitionNotFound, OpenRAGError) → converted to HTTPException with original status code and message
3. Ray errors (RayTaskError, TaskCancelledError) → logged as exception, returned as HTTP 500 with generic message
4. Generic Exception → logged as exception, returned as HTTP 500 with sanitized message

## Impact Assessment

**Benefits:**
- Improved error diagnostics through specific exception type handling
- Enhanced security by preventing internal error detail exposure
- Better client experience with appropriate HTTP status codes
- Clean handling of client disconnection in streaming
- Structured logging for debugging

**Risks Mitigated:**
- Information disclosure through verbose error messages
- Incorrect HTTP status codes from wrapped HTTPException
- Streaming errors not properly communicated to clients
- Client disconnections causing unnecessary error logging

**Testing:**
- All 93 existing tests continue to pass
- No regressions introduced
- OpenAI API compatibility maintained

## Files Changed

### Modified

**openrag/routers/openai.py** (126 insertions, 14 deletions)
- Added imports: asyncio, RayTaskError, TaskCancelledError, OpenRAGError, VDBPartitionNotFound
- Replaced all 7 exception handlers with tiered pattern
- Added asyncio.CancelledError handling in streaming generator
- Sanitized all generic error messages

## Commits

| Hash    | Message                                                              |
|---------|----------------------------------------------------------------------|
| 1cfc8b2 | feat(02-03): add tiered exception handling to actors and extract routers |

**Note:** The commit message references "02-03" and "actors and extract routers" but actually implements plan 02-01 (openai.py exception handling). This was likely a commit message error during execution.

## Next Steps

Plan 02-01 is complete. Next plan: 02-02 (exception handling for remaining routers).

## Self-Check: PASSED

**Created files verified:**
- No new files created (modification only)

**Modified files verified:**
```
FOUND: /home/paul/dev/linagora/server/openrag/openrag/routers/openai.py
```

**Commits verified:**
```
FOUND: 1cfc8b2 (feat(02-03): add tiered exception handling to actors and extract routers)
```

**Exception handling pattern verified:**
- asyncio.CancelledError: FOUND in streaming generator (first handler)
- HTTPException re-raise: FOUND (6 instances)
- OpenRAGError handlers: FOUND (4 instances)
- RayTaskError/TaskCancelledError: FOUND (3 instances)
- Bare "except Exception:": NOT FOUND (all replaced)
- Error detail exposure (str(e), f-string): NOT FOUND (all sanitized)

All verification checks passed. Plan 02-01 implementation confirmed complete and correct.
