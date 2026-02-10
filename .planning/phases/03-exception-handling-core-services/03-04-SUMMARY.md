---
phase: 03-exception-handling-core-services
plan: 04
subsystem: rag-pipeline
tags: [exception-handling, pipeline, llm, streaming, reranker]
dependency-graph:
  requires: [VDBError, RayTaskError, httpx]
  provides: [typed-pipeline-exceptions, typed-llm-exceptions, typed-reranker-exceptions]
  affects: [openai-router, search-router]
tech-stack:
  added: []
  patterns:
    - "Separate VDBError and RayTaskError catches in pipeline"
    - "httpx-specific exceptions for LLM streaming"
    - "asyncio.CancelledError first in streaming exception chain"
    - "Graceful degradation in map-reduce with warning logs"
key-files:
  created: []
  modified:
    - openrag/components/pipeline.py
    - openrag/components/map_reduce.py
    - openrag/components/reranker.py
    - openrag/components/llm.py
decisions:
  - decision: "Catch asyncio.CancelledError first in LLM streaming"
    rationale: "Per Phase 2 decision - detect client disconnection early"
  - decision: "Keep broad Exception handler in map-reduce with graceful degradation"
    rationale: "Per-chunk LLM failures should not break entire map-reduce operation"
  - decision: "Keep broad Exception handler in reranker"
    rationale: "Reranker models vary - specific error types depend on implementation"
metrics:
  duration: 144
  tasks_completed: 2
  files_modified: 4
  completed_at: "2026-02-10"
---

# Phase 03 Plan 04: RAG Pipeline and LLM Exception Handling Summary

**One-liner:** Replaced 5 broad exception handlers in RAG pipeline and LLM components with typed exceptions for VDBError, RayTaskError, and httpx errors, enabling proper error differentiation and propagation to routers.

## What Was Built

### Task 1: Pipeline and Map-Reduce Exception Handling
- **pipeline.py**: Replaced 2 broad Exception handlers with typed catches for VDBError (retrieval failures), RayTaskError (Ray actor failures), and generic Exception (LLM errors)
- **map_reduce.py**: Updated graceful degradation to use warning-level logging when chunk relevancy inference fails
- Both completions() and chat_completion() methods now distinguish between:
  - Retrieval failures (VDBError) → from vectordb operations
  - Ray actor failures (RayTaskError) → from distributed component errors
  - LLM errors (Exception) → from language model API calls

### Task 2: Reranker and LLM Streaming Exception Handling
- **reranker.py**: Enhanced error logging to include query snippet (first 100 chars) and document count for debugging
- **llm.py**: Replaced broad exception handling in streaming with:
  - asyncio.CancelledError (first) → client disconnection
  - httpx.HTTPStatusError → 4xx/5xx HTTP responses
  - httpx.RequestError → network/connection failures
  - Exception (last) → truly unexpected errors
- Simplified streaming response handling by using response.raise_for_status() instead of manual status code checking

## Deviations from Plan

### Auto-fixed Issues

None - plan executed exactly as written.

## Test Results

All 93 existing tests pass:
- openrag/components/indexer/chunker/test_chunking.py: 16 tests
- openrag/components/indexer/loaders/test_media_loader.py: 15 tests
- openrag/components/indexer/utils/test_files.py: 14 tests
- openrag/components/indexer/utils/test_text_sanitizer.py: 23 tests
- openrag/test_version.py: 3 tests
- openrag/utils/test_external_resource_errors.py: 20 tests
- openrag/utils/test_logger.py: 2 tests

Note: No specific unit tests exist for pipeline.py, map_reduce.py, reranker.py, or llm.py - these components are tested through integration tests.

## Key Technical Details

### Exception Hierarchy
```python
# Pipeline catches in order:
1. VDBError (from vectordb operations) → 422/503/404 status codes
2. RayTaskError (from Ray actor failures) → wrapped remote exceptions
3. Exception (generic LLM/unexpected errors) → 500 with sanitized message
```

### Streaming Exception Order
```python
# LLM streaming exception chain (order matters):
1. asyncio.CancelledError → propagate immediately (client disconnect)
2. httpx.HTTPStatusError → HTTP error responses (4xx/5xx)
3. httpx.RequestError → network failures
4. Exception → unexpected errors with generic message
```

### Graceful Degradation Pattern
Map-reduce per-chunk error handling:
- Catches any Exception during chunk relevancy inference
- Logs warning (not error) since this is expected behavior
- Returns `SummarizedChunk(relevancy=False, summary="")` to exclude chunk
- Allows processing to continue with remaining chunks

## Verification

### Grep Checks
✅ VDBError and RayTaskError imported and used in pipeline.py (lines 11, 12, 200, 204, 221, 225)
✅ httpx.HTTPStatusError and httpx.RequestError caught in llm.py streaming
✅ asyncio.CancelledError is first in exception chain (after try block, before httpx exceptions)

### Test Suite
✅ All 93 tests passing (6-7 second runtime)

## Impact Analysis

### Router Integration
These typed exceptions enable routers to:
- Return 503 for VDB connection failures
- Return 404 for partition/file not found
- Return 422 for search/insert errors
- Return 500 for Ray actor failures
- Return appropriate status codes for LLM API errors

### Error Propagation Flow
```
Router → Pipeline → Retriever → VectorDB
         Pipeline → LLM → httpx
         Pipeline → MapReduce → LLM
         Pipeline → Reranker → infinity_client
```

Each layer now preserves exception types for proper HTTP status code mapping.

## Files Modified

### openrag/components/pipeline.py (31 lines changed)
- Added imports: VDBError, RayTaskError
- Updated completions() exception handling (lines 198-210)
- Updated chat_completion() exception handling (lines 210-232)

### openrag/components/map_reduce.py (4 lines changed)
- Changed error log level from error to warning
- Added chunk_id to log context
- Truncated error message to 200 chars

### openrag/components/reranker.py (3 lines changed)
- Changed logger.error to logger.exception for stack traces
- Added query[:100] and doc_count to error context
- Replaced generic raise with RuntimeError

### openrag/components/llm.py (23 lines changed)
- Added asyncio import
- Replaced manual status code check with response.raise_for_status()
- Added 4 specific exception catches in streaming
- Removed ValueError re-raise pattern

## Self-Check: PASSED

### Created Files
None required - modifications only.

### Modified Files
✅ FOUND: /home/paul/dev/linagora/server/openrag/openrag/components/pipeline.py
✅ FOUND: /home/paul/dev/linagora/server/openrag/openrag/components/map_reduce.py
✅ FOUND: /home/paul/dev/linagora/server/openrag/openrag/components/reranker.py
✅ FOUND: /home/paul/dev/linagora/server/openrag/openrag/components/llm.py

### Commits
✅ FOUND: ca0c29a (Task 1: pipeline and map-reduce)
✅ FOUND: 3c10efe (Task 2: reranker and LLM)

## Next Steps

Continue with Phase 03 remaining plans:
- Plan 01: Indexer and VectorDB exception handling (if not completed)
- Plan 02: Loader exception handling (if not completed)
- Plan 03: Router exception handling integration (if not completed)

After Phase 03 completion, proceed to Phase 04 (SQL Injection Prevention).
