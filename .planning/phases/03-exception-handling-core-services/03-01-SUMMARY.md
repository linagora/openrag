---
phase: 03-exception-handling-core-services
plan: 01
subsystem: vectordb-metadata
tags: [exception-handling, milvus, postgresql, vectordb]
dependency-graph:
  requires: [utils/exceptions/vectordb.py]
  provides: [typed-exceptions-vectordb, typed-exceptions-metadata]
  affects: [routers/indexer.py, routers/search.py, routers/partition.py]
tech-stack:
  added: []
  patterns: [exception-chaining, typed-exceptions, generic-error-messages]
key-files:
  created: []
  modified:
    - openrag/components/indexer/vectordb/vectordb.py
    - openrag/components/indexer/vectordb/utils.py
decisions:
  - Use generic message "An unexpected database error occurred" for all Exception catch-all handlers
  - Catch MilvusException before generic Exception in all Milvus operations
  - Wrap SQLAlchemy exceptions in appropriate VDBError subclasses based on operation type
  - Propagate VDBError subclasses in existence check methods instead of swallowing errors
metrics:
  duration: 5min
  completed: 2026-02-10
---

# Phase 03 Plan 01: Vectordb Exception Handling Summary

**One-liner:** Replace 17 broad exception handlers in vector database and metadata management with typed VDBError subclasses that distinguish Milvus operations, PostgreSQL operations, and connection failures.

## What Was Done

### Task 1: Replace 13 exception handlers in vectordb.py ✓

Updated MilvusDB Ray actor to use specific exception types:

**Connection/Initialization (2 handlers):**
- Line 178 `__init__()`: Catch MilvusException → VDBConnectionError, generic Exception → VDBConnectionError with non-exposing message
- Line 244 `load_collection()`: Already catching MilvusException correctly for create/load, added UnexpectedVDBError for generic catch-all

**Insert Operations (1 handler):**
- Line 413 `async_add_documents()`: Added MilvusException handler → VDBInsertError, updated generic Exception → UnexpectedVDBError

**Search Operations (3 handlers):**
- Line 585 `_search_with_filter()`: Already catching MilvusException → VDBSearchError correctly, updated generic Exception → UnexpectedVDBError
- Line 717 `get_file_chunks()`: Already catching MilvusException → VDBSearchError correctly, updated generic Exception → UnexpectedVDBError
- Line 761 `get_chunk_by_id()`: Already catching MilvusException → VDBSearchError correctly, updated generic Exception → UnexpectedVDBError

**Delete Operations (2 handlers):**
- Line 659 `delete_file()`: Already catching MilvusException → VDBDeleteError correctly, updated generic Exception → UnexpectedVDBError
- Line 838 `delete_partition()`: Already catching MilvusException → VDBDeleteError correctly, updated generic Exception → UnexpectedVDBError

**List/Query Operations (3 handlers):**
- Line 791 `list_partition_files()`: Updated generic Exception → UnexpectedVDBError
- Line 805 `list_partitions()`: Added VDBError propagation, updated generic Exception → UnexpectedVDBError
- Line 917 `list_all_chunk()`: Already catching MilvusException → VDBSearchError correctly, updated generic Exception → UnexpectedVDBError

**Existence Checks (2 handlers):**
- Line 774 `file_exists()`: Changed from swallowing errors to propagating VDBError and raising UnexpectedVDBError
- Line 853 `partition_exists()`: Changed from swallowing errors to propagating VDBError and raising UnexpectedVDBError

**Pattern applied:** All handlers now follow the structure:
1. Catch specific exceptions first (MilvusException, EmbeddingError, VDBError)
2. Wrap in appropriate VDBError subclass
3. Catch generic Exception last with non-exposing message

### Task 2: Replace 4 exception handlers in utils.py ✓

Updated PartitionFileManager class PostgreSQL operations:

**Connection (1 handler):**
- Line 155 `__init__()`: Updated VDBConnectionError to use generic message instead of exposing error details

**Insert Operations (2 handlers):**
- Line 246 `add_file_to_partition()`: Changed from re-raise to VDBInsertError with generic message
- Line 336 `set_file_domains()`: Changed from re-raise to VDBInsertError with generic message

**Delete Operations (1 handler):**
- Line 265 `remove_file_from_partition()`: Changed from re-raise to VDBDeleteError with generic message

All SQLAlchemy exceptions now wrapped in appropriate VDBError subclasses based on the operation type (connection, insert, delete).

## Deviations from Plan

None - plan executed exactly as written.

## Technical Changes

**Exception Handler Updates:**
- Total handlers replaced: 17 (13 in vectordb.py, 4 in utils.py)
- All exception messages standardized to: "An unexpected database error occurred"
- MilvusException now caught explicitly in 10 locations
- VDBError subclasses now propagated in existence check methods

**Exception Flow:**
```
MilvusDB method
  ↓
try: Milvus/SQLAlchemy operation
  ↓
except MilvusException → VDBSearchError/VDBInsertError/VDBDeleteError/etc
except EmbeddingError → propagate
except VDBError → propagate
except Exception → UnexpectedVDBError (generic message)
```

**Key Implementation Details:**
1. MilvusException already imported at line 16 of vectordb.py
2. VDBError subclasses imported via wildcard: `from utils.exceptions.vectordb import *`
3. Error context preserved in structured logging but not exposed in exception messages
4. All operations maintain existing Ray actor exception propagation semantics

## Verification

**Tests:** All 93 unit tests pass
```bash
uv run pytest openrag/ -v
============================== 93 passed in 4.37s ==============================
```

**Coverage:** No test-specific changes needed - exception wrapping is transparent to callers

**Router Integration:** Routers continue to catch VDBError base class and map to appropriate HTTP status codes via the status_code attribute

## Impact

**Observability Improvements:**
- Database operations now distinguishable by exception type
- Milvus failures clearly separated from PostgreSQL failures
- Connection errors distinct from operation errors

**Router Benefits:**
- Can now return specific HTTP status codes based on VDBError subclass:
  - VDBConnectionError → 503 Service Unavailable
  - VDBInsertError → 422 Unprocessable Entity
  - VDBSearchError → 422 Unprocessable Entity
  - VDBDeleteError → 422 Unprocessable Entity
  - VDBPartitionNotFound → 404 Not Found
  - VDBFileNotFoundError → 404 Not Found
  - UnexpectedVDBError → 500 Internal Server Error

**Security:**
- All generic Exception catch-alls use non-exposing messages
- Error details logged but not propagated to clients
- Follows Phase 2 decisions on error message handling

## Files Modified

| File | Lines Changed | Handlers Replaced |
|------|---------------|-------------------|
| openrag/components/indexer/vectordb/vectordb.py | +38 -14 | 13 |
| openrag/components/indexer/vectordb/utils.py | +85 -7 | 4 |

## Commits

- `ab5ec5b` refactor(03-01): replace exception handlers in vectordb.py with typed VDBError subclasses
- `cc39b4a` refactor(03-01): replace exception handlers in utils.py with typed VDBError subclasses

## Next Steps

This completes Phase 3 Plan 1. The vectordb and metadata management components now have fully typed exception handling. Next plans will address:
- Plan 02: Indexer and chunker exception handling
- Plan 03: Loader exception handling
- Plan 04: RAG pipeline exception handling

## Self-Check: PASSED

**Created files verified:**
- N/A - no new files created

**Modified files verified:**
```bash
[ -f "openrag/components/indexer/vectordb/vectordb.py" ] && echo "FOUND: vectordb.py"
FOUND: vectordb.py

[ -f "openrag/components/indexer/vectordb/utils.py" ] && echo "FOUND: utils.py"
FOUND: utils.py
```

**Commits verified:**
```bash
git log --oneline --all | grep -q "ab5ec5b" && echo "FOUND: ab5ec5b"
FOUND: ab5ec5b

git log --oneline --all | grep -q "cc39b4a" && echo "FOUND: cc39b4a"
FOUND: cc39b4a
```

All files and commits verified successfully.
