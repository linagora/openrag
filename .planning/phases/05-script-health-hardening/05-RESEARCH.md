# Phase 5: Script & Health Hardening - Research

**Researched:** 2026-02-11
**Domain:** FastAPI health checks with external service monitoring, Python script error handling with transaction rollback
**Confidence:** HIGH

## Summary

Phase 5 enhances observability and resilience through two parallel efforts: adding comprehensive health checks to monitor LLM/VLM service availability with response time metrics, and hardening the restore script with proper rollback on critical failures and detailed progress logging.

The FastAPI health check ecosystem offers several mature libraries (`fastapi-health`, `fastapi-healthchecks`) but they add dependencies. The codebase already uses `httpx` extensively for external service calls and Loguru for structured logging, making a custom implementation with existing tools the most appropriate choice. For the restore script, SQLAlchemy's session management with explicit `rollback()` in exception handlers and Loguru's structured logging provide the necessary rollback and observability capabilities.

**Primary recommendation:** Implement custom health check endpoint using existing httpx/Loguru stack rather than adding new dependencies. Enhance restore script with structured error tracking, progress counters, and explicit rollback logic in existing try/except blocks.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| httpx | Already in use | Async HTTP client for health probes | Already used throughout codebase (llm.py, app_front.py), has built-in elapsed time tracking |
| Loguru | Already in use | Structured logging with context binding | Codebase standard (utils/logger.py), supports JSON output, context binding with .bind() |
| SQLAlchemy | Already in use | Database session management with rollback | Already used in vectordb/utils.py, provides transaction control |
| asyncio | stdlib (Python 3.9+) | Async primitives for concurrent health checks | No dependency, required for async endpoint |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| time | stdlib | Response time measurement | Fallback if httpx.Response.elapsed unavailable |
| FastAPI JSONResponse | Already in use | Structured health check response | Standard FastAPI response formatting |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Custom health check | fastapi-health (PyPI) | Adds dependency for simple use case; codebase pattern uses minimal deps |
| Custom health check | fastapi-healthchecks (PyPI) | Heavier library with many built-in checks we don't need (Redis, RabbitMQ, etc.) |
| Manual error tracking | proglog library | Adds dependency; Loguru already provides structured logging |
| Manual rollback | Unit of Work pattern library | Over-engineering for simple restore script; SQLAlchemy session.rollback() sufficient |

**Installation:**
```bash
# No new dependencies required
# All needed libraries already in use
```

## Architecture Patterns

### Recommended Health Check Endpoint Structure

Based on FastAPI health check best practices and Kubernetes probe patterns:

```python
# Source: Kubernetes liveness/readiness pattern
@app.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request):
    """
    Enhanced health check reporting LLM/VLM availability with response times.
    Returns 200 if all services healthy, 503 if any critical service unavailable.
    """
    from components.utils import get_llm_clients
    import httpx
    import time

    config = request.app.state.app_state.config

    checks = {
        "api": {"status": "healthy"},
        "llm": await check_service_health(config.llm.base_url, "LLM"),
        "vlm": await check_service_health(config.vlm.base_url, "VLM"),
    }

    all_healthy = all(check["status"] == "healthy" for check in checks.values())

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={
            "status": "healthy" if all_healthy else "degraded",
            "checks": checks,
            "timestamp": time.time(),
        }
    )

async def check_service_health(base_url: str, service_name: str) -> dict:
    """Check external service health with response time tracking."""
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            response = await client.get(f"{base_url}/health")  # or /v1/models
            elapsed_ms = (time.time() - start) * 1000

        if response.status_code == 200:
            return {
                "status": "healthy",
                "response_time_ms": round(elapsed_ms, 2),
            }
        else:
            return {
                "status": "unhealthy",
                "error": f"HTTP {response.status_code}",
                "response_time_ms": round(elapsed_ms, 2),
            }
    except httpx.TimeoutException:
        return {"status": "timeout", "error": "Service did not respond within 3s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
```

### Pattern 1: Health Check with Service Availability

**What:** Probe external LLM/VLM endpoints to verify they're reachable and responding

**When to use:** Production deployments where LLM service failures should be detected before user requests fail

**Key considerations:**
- Timeout should be short (3s) to avoid blocking health check endpoint itself
- Don't fail health check if non-critical services are down (return 200 with degraded status)
- Cache results briefly (10-30s) to avoid overwhelming services with health probes
- Use async/await to check multiple services concurrently

**Example:**
```python
# Concurrent health checks with asyncio.gather
async def health_check():
    llm_check, vlm_check = await asyncio.gather(
        check_service_health(config.llm.base_url, "LLM"),
        check_service_health(config.vlm.base_url, "VLM"),
        return_exceptions=True,  # Don't fail if one service check raises
    )
    # ...
```

### Pattern 2: Restore Script Error Tracking and Rollback

**What:** Track operation progress, detect critical failures, and rollback partial changes

**When to use:** Any script that makes multi-step database changes where partial completion leaves inconsistent state

**Current implementation (restore.py):**
```python
# Source: openrag/scripts/restore.py:295-330
try:
    with open_backup_file(args.input, logger) as fh:
        added_documents = {}

        for line in fh:
            line = line.strip()

            if line in ["rdb"]:
                read_rdb_section(...)  # Can raise Exception

            if line in ["vdb"]:
                read_vdb_section(...)  # Can raise Exception
except Exception as e:
    logger.error("Error: " + str(e))
    raise
finally:
    client.close()
```

**Enhanced pattern with rollback and progress tracking:**
```python
# Track progress and failures
restore_state = {
    "partitions_created": [],
    "files_added": 0,
    "files_failed": 0,
    "errors": [],
}

try:
    with open_backup_file(args.input, logger) as fh:
        added_documents = {}

        for line in fh:
            line = line.strip()

            if line in ["rdb"]:
                # Pass restore_state to track progress
                read_rdb_section(fh, pfm, ..., restore_state, logger, ...)

            if line in ["vdb"]:
                read_vdb_section(fh, ..., restore_state, logger, ...)

    # Success - log summary
    logger.info(
        "Restore completed successfully",
        partitions=len(restore_state["partitions_created"]),
        files_added=restore_state["files_added"],
        files_failed=restore_state["files_failed"],
    )

except Exception as e:
    # Critical failure - rollback
    logger.error(
        "Critical restore failure - rolling back",
        error=str(e),
        files_added=restore_state["files_added"],
        files_failed=restore_state["files_failed"],
    )

    # Rollback: delete partitions created during this restore
    for partition_name in restore_state["partitions_created"]:
        try:
            logger.info(f"Rolling back partition: {partition_name}")
            pfm.delete_partition(partition_name)
            # Also delete from Milvus
            client.delete(
                collection_name=vdb["collection_name"],
                filter=f'partition == "{partition_name}"',
            )
        except Exception as rollback_error:
            logger.exception(f"Rollback failed for partition {partition_name}")

    # Re-raise to signal failure
    raise

finally:
    client.close()
```

### Pattern 3: Structured Progress Logging

**What:** Log operation progress with counters and contextual information

**When to use:** Long-running operations where users need visibility into what's happening

**Loguru structured logging pattern (already in use):**
```python
# Source: Existing pattern from vectordb/utils.py
logger.bind(file_id=file_id, partition=partition).info("Added file successfully")

# Enhanced for restore script
logger.bind(
    partition=partition_name,
    files_processed=restore_state["files_added"],
    files_failed=restore_state["files_failed"],
).info(f"Processing RDB section")

# Error summary at end
logger.bind(
    total_files=restore_state["files_added"] + restore_state["files_failed"],
    successful=restore_state["files_added"],
    failed=restore_state["files_failed"],
    error_summary=restore_state["errors"][:10],  # First 10 errors
).error("Restore completed with errors")
```

### Pattern 4: Graceful Degradation in Health Checks

**What:** Return partial health status even if some checks fail

**When to use:** Systems with multiple dependencies where some are critical and others optional

```python
# Critical vs non-critical services
checks = {
    "api": {"status": "healthy", "critical": True},
    "vectordb": await check_vectordb_health(),  # Critical
    "llm": await check_service_health(llm_url),  # Critical for RAG
    "vlm": await check_service_health(vlm_url),  # Non-critical (only for image captioning)
}

critical_healthy = all(
    check["status"] == "healthy"
    for check in checks.values()
    if check.get("critical", False)
)

# Return 200 if critical services healthy, even if VLM is down
status_code = 200 if critical_healthy else 503
overall_status = "healthy" if critical_healthy else "degraded"
```

### Anti-Patterns to Avoid

- **Don't block health check endpoint** - Use short timeouts (3s max) and async calls to avoid slow health checks blocking the API
- **Don't swallow exceptions without rollback** - Any critical failure must trigger rollback before re-raising
- **Don't log every file individually at INFO level** - Use DEBUG for per-file logs, INFO for progress milestones (every 100 files, partition completion)
- **Don't make health check dependent on auth** - Health check endpoint must bypass authentication middleware (already excluded in line 122 of api.py)
- **Don't rollback on every error** - Distinguish between critical failures (stop and rollback) and non-critical errors (log and continue)

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP response time measurement | Manual time.time() wrapper | `response.elapsed.total_seconds()` (httpx built-in) | Accurate, accounts for DNS/connection/response time separately |
| Structured logging | String formatting with print() | Loguru with `.bind()` | Already in codebase, supports JSON output, context propagation |
| Transaction rollback | Manual state tracking with undo operations | SQLAlchemy `session.rollback()` | Handles FK constraints, cascades, partial commits automatically |
| Health check caching | Manual timestamp checking | TTLCache (if needed) or simple dict with timestamp | Thread-safe, TTL expiration, LRU eviction |
| Async timeouts | Manual asyncio.wait_for with cleanup | httpx.Timeout built-in | Properly cancels requests, releases resources |

**Key insight:** The codebase already has all necessary primitives (httpx for probes, Loguru for logging, SQLAlchemy for rollback). Don't add dependencies for functionality we can achieve with existing tools.

## Common Pitfalls

### Pitfall 1: Health Check Timeout Cascade

**What goes wrong:** Health check endpoint times out while waiting for slow external service, blocking the health check itself and cascading to orchestrator timeouts.

**Why it happens:** No timeout on external service probe, or timeout too long (30s+), blocking the health check endpoint.

**How to avoid:**
- Set aggressive timeout on external probes (3s recommended)
- Use `httpx.Timeout(3.0)` to enforce hard deadline
- Return degraded status immediately on timeout, don't retry

```python
# Bad - no timeout
async with httpx.AsyncClient() as client:
    response = await client.get(f"{base_url}/health")  # Could hang forever

# Good - explicit short timeout
async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
    response = await client.get(f"{base_url}/health")  # Fails fast
```

**Warning signs:** Kubernetes liveness probe failing, health check endpoint showing high p99 latency, cascading timeouts in monitoring.

### Pitfall 2: Partial Rollback Leaves Inconsistent State

**What goes wrong:** Script rolls back RDB (PostgreSQL) changes but not VDB (Milvus) changes, leaving orphaned vectors in Milvus pointing to non-existent files in PostgreSQL.

**Why it happens:** Rollback logic only addresses one database, or rollback itself fails partway through.

**How to avoid:**
- Track all operations that need rollback (both RDB and VDB)
- Implement rollback in reverse order of operations (VDB first, then RDB)
- Wrap rollback in try/except to ensure partial rollback doesn't stop cleanup

```python
# Good - rollback both databases
except Exception as e:
    logger.error("Critical failure - rolling back")

    # Rollback in reverse order
    for partition_name in reversed(restore_state["partitions_created"]):
        try:
            # 1. Delete from VDB first (no FK constraints)
            client.delete(
                collection_name=vdb["collection_name"],
                filter=f'partition == "{partition_name}"',
            )
        except Exception as vdb_error:
            logger.exception(f"VDB rollback failed for {partition_name}")

        try:
            # 2. Delete from RDB (cascades to files via FK)
            pfm.delete_partition(partition_name)
        except Exception as rdb_error:
            logger.exception(f"RDB rollback failed for {partition_name}")

    raise
```

**Warning signs:** Database inconsistency errors on subsequent operations, orphaned data, file count mismatches between PostgreSQL and Milvus.

### Pitfall 3: Progress Logging Performance Impact

**What goes wrong:** Logging every file operation at INFO level generates massive log volume, impacting performance and filling disk.

**Why it happens:** Desire for detailed visibility leads to over-logging without considering volume (restore might process 10k+ files).

**How to avoid:**
- Log at DEBUG level for per-file operations
- Log at INFO level only for milestones (every 100 files, partition completion, final summary)
- Use structured logging with counters instead of individual messages

```python
# Bad - logs 10,000 lines for 10k files
for doc in documents:
    logger.info(f"Processing file {doc['file_id']}")  # ❌ Too verbose

# Good - logs ~100 lines for 10k files
files_processed = 0
for doc in documents:
    files_processed += 1
    if files_processed % 100 == 0:
        logger.info(f"Progress: {files_processed} files processed")  # ✓ Milestone only

# Even better - structured logging with final summary
logger.info(
    "Partition restore complete",
    partition=partition_name,
    files_added=len(added_files),
    duration_sec=elapsed_time,
)
```

**Warning signs:** Log file size growing to GB during restore, slow restore performance, disk space warnings.

### Pitfall 4: Ignoring SQLAlchemy Session State After Error

**What goes wrong:** After an exception with rollback, session is in invalid state and subsequent operations fail with "Can't reconnect until invalid transaction is rolled back" error.

**Why it happens:** Exception occurs but rollback() not called, or rollback() called but session not properly closed/recreated.

**How to avoid:**
- Always use context manager (`with self.Session() as session:`) which handles cleanup
- Call `session.rollback()` explicitly in exception handler
- Don't reuse session after exception - create new session for retry

```python
# Bad - session not properly cleaned up
session = self.Session()
try:
    session.add(file)
    session.commit()
except Exception as e:
    # Missing rollback!
    raise

# Good - context manager handles cleanup
with self.Session() as session:
    try:
        session.add(file)
        session.commit()
    except Exception as e:
        session.rollback()  # Explicit rollback
        raise
# Context manager closes session even if exception raised
```

**Warning signs:** "Invalid transaction" errors, "Can't reconnect" errors, subsequent database operations failing after first error.

### Pitfall 5: Health Check Returning 200 When Services Actually Down

**What goes wrong:** Health check endpoint returns 200 OK even when LLM service is unreachable, hiding failures from orchestrator.

**Why it happens:** Exception handling swallows errors without updating health status, or check logic doesn't properly interpret service responses.

**How to avoid:**
- Distinguish between endpoint returning 200 (service exists and responded) vs 404/500 (service unhealthy)
- Use status code from health response to determine overall status
- Don't swallow exceptions - translate to unhealthy status

```python
# Bad - always returns 200
@app.get("/health_check")
async def health_check():
    try:
        response = await client.get(f"{llm_url}/health")
        # ❌ Returns 200 even if LLM returned 500!
        return {"status": "ok"}
    except Exception:
        return {"status": "ok"}  # ❌ Hides failures!

# Good - return 503 if any critical service unhealthy
@app.get("/health_check")
async def health_check():
    checks = {
        "llm": await check_service_health(llm_url),
    }

    all_healthy = all(c["status"] == "healthy" for c in checks.values())

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={"status": "healthy" if all_healthy else "degraded", "checks": checks},
    )
```

**Warning signs:** Kubernetes not restarting unhealthy pods, users reporting errors but health check shows green, monitoring alerts not firing.

## Code Examples

Verified patterns from official sources and codebase:

### httpx Response Time Measurement

```python
# Source: httpx documentation + time module
import httpx
import time

# Method 1: httpx built-in elapsed (recommended)
async with httpx.AsyncClient() as client:
    response = await client.get(url)
    elapsed_ms = response.elapsed.total_seconds() * 1000

# Method 2: manual timing (if httpx.elapsed unavailable)
start = time.time()
async with httpx.AsyncClient() as client:
    response = await client.get(url)
elapsed_ms = (time.time() - start) * 1000
```

### Loguru Structured Logging (Existing Codebase)

```python
# Source: openrag/utils/logger.py, openrag/components/indexer/vectordb/utils.py
from utils.logger import get_logger

logger = get_logger()

# Bind context
log = logger.bind(file_id=file_id, partition=partition)
log.info("Added file successfully")

# Multi-field binding
logger.bind(
    files_processed=100,
    files_failed=2,
    partition=partition_name,
).info("Restore progress milestone")
```

### SQLAlchemy Rollback Pattern (Existing Codebase)

```python
# Source: openrag/components/indexer/vectordb/utils.py:246-253
with self.Session() as session:
    try:
        # Database operations
        session.add(file)
        session.commit()
        log.info("Added file successfully")
        return True
    except Exception as e:
        session.rollback()
        log.exception("Error adding file to partition", error=str(e))
        raise VDBInsertError(...)
```

### Concurrent Health Checks with asyncio.gather

```python
# Source: asyncio documentation + FastAPI pattern
import asyncio

async def health_check():
    # Run all checks concurrently
    results = await asyncio.gather(
        check_service_health(config.llm.base_url, "LLM"),
        check_service_health(config.vlm.base_url, "VLM"),
        check_vectordb_health(),
        return_exceptions=True,  # Don't fail if one check raises
    )

    checks = {
        "llm": results[0],
        "vlm": results[1],
        "vectordb": results[2],
    }

    # ... evaluate overall health
```

### Restore Script Error Summary

```python
# Pattern: Collect errors during processing, summarize at end
restore_state = {
    "errors": [],  # List of (file_id, partition, error_message) tuples
}

# During processing
try:
    pfm.add_file_to_partition(doc["file_id"], partition, doc, user_id)
except Exception as e:
    restore_state["errors"].append({
        "file_id": doc["file_id"],
        "partition": partition,
        "error": str(e),
    })

# At end - log summary
if restore_state["errors"]:
    logger.error(
        "Restore completed with errors",
        total_errors=len(restore_state["errors"]),
        first_10_errors=restore_state["errors"][:10],  # Don't overflow logs
    )
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Simple `{"status": "ok"}` health check | Structured health with service checks and metrics | 2023+ (Cloud Native patterns) | Enables detailed monitoring, gradual degradation detection |
| Manual exception logging with print() | Structured logging with Loguru/context binding | 2020+ (Loguru popularity) | Machine-parseable logs, better observability |
| String concatenation error messages | Exception chaining with context | Python 3.11+ (exception groups) | Preserves full error context, better debugging |
| response.elapsed (requests library) | response.elapsed (httpx library) | 2021+ (httpx adoption) | Same API, but httpx is async-native |
| Manual rollback with delete operations | SQLAlchemy session.rollback() | SQLAlchemy 1.4+ (2020) | Automatic cleanup, handles FK constraints |

**Deprecated/outdated:**
- Basic health checks without service dependency validation (pre-2020 pattern)
- Using `requests` library for async operations (httpx is async-native replacement)
- Manual error state tracking without structured logging (Loguru provides better alternative)
- Global rollback flag without tracking what needs rollback (error-prone, misses edge cases)

## Open Questions

1. **Should health check cache results to avoid overwhelming LLM/VLM services?**
   - What we know: Health check will be called frequently by Kubernetes probes (every 10s typical)
   - What's unclear: Whether LLM/VLM services can handle probe frequency, or need caching with TTL
   - Recommendation: Start without caching; add 10-30s TTL cache if probe traffic becomes issue

2. **What constitutes a "critical failure" in restore script that requires rollback?**
   - What we know: Current code raises exceptions on parse errors, database errors
   - What's unclear: Should individual file failures stop entire restore, or only systemic failures?
   - Recommendation: Define critical failures as: parse errors (bad backup file), connection errors (database unavailable), but NOT individual file insert failures (log and continue)

3. **Should VLM be considered critical or optional in health check?**
   - What we know: VLM only used for image captioning in loaders (config.loader.image_captioning)
   - What's unclear: Whether system should report unhealthy if VLM down but LLM operational
   - Recommendation: Mark VLM as non-critical; return 200 with degraded status if only VLM down, 503 if LLM down

4. **Should restore script support resume-from-checkpoint if partially complete?**
   - What we know: Current implementation rolls back on failure, no checkpoint support
   - What's unclear: Whether resume capability needed for very large restores (hours long)
   - Recommendation: Out of scope for Phase 5; log this as future enhancement if large restores become common

## Sources

### Primary (HIGH confidence)
- [FastAPI Health Check Endpoint Example - Index.dev](https://www.index.dev/blog/how-to-implement-health-check-in-python)
- [SQLAlchemy Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)
- [Python httpx Response Time Measurement](https://number1.co.za/python-check-how-long-a-httpx-request-took-to-run/)
- Codebase: `openrag/api.py` (existing health_check endpoint at line 189-192)
- Codebase: `openrag/components/llm.py` (httpx usage patterns for external service calls)
- Codebase: `openrag/scripts/restore.py` (current error handling and logging)
- Codebase: `openrag/components/indexer/vectordb/utils.py` (SQLAlchemy rollback pattern)
- Codebase: `openrag/utils/logger.py` (Loguru configuration and structured logging)

### Secondary (MEDIUM confidence)
- [fastapi-health PyPI](https://pypi.org/project/fastapi-health/)
- [fastapi-healthchecks PyPI](https://pypi.org/project/fastapi-healthchecks/)
- [Mastering Cleanup Actions in Python Error Handling](https://pythondeck.com/cleanup_actions_during_error_handling.php)
- [How to Handle Exceptions Properly in Python (2026)](https://oneuptime.com/blog/post/2026-01-24-handle-exceptions-properly-python/view)
- [Python Logging Best Practices - Medium](https://medium.lies.io/progress-bar-and-status-logging-in-python-with-tqdm-35ce29b908f5)

### Tertiary (LOW confidence)
- Web search results about health check patterns (general guidance, not authoritative)
- Medium articles on error handling (examples, not official documentation)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - httpx, Loguru, SQLAlchemy already in use; patterns verified in codebase
- Architecture: HIGH - Health check pattern well-documented; rollback pattern in existing code (utils.py:246-253)
- Pitfalls: HIGH - Timeout cascade, partial rollback, and session state issues documented in official SQLAlchemy docs and codebase experience

**Research date:** 2026-02-11
**Valid until:** 2026-04-11 (60 days - FastAPI and SQLAlchemy patterns are stable; httpx API unlikely to change)
