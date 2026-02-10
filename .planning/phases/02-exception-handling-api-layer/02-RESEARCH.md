# Phase 2: Exception Handling - API Layer - Research

**Researched:** 2026-02-10
**Domain:** FastAPI exception handling and error response architecture
**Confidence:** HIGH

## Summary

This research covers replacing 19 bare `except Exception` handlers across OpenRAG's API router files with specific exception types. The codebase already has a well-designed exception hierarchy (`OpenRAGError` with VDB and Embedding subclasses), a global exception handler in `api.py`, and Ray-specific error types. The primary challenge is handling streaming responses (SSE) where errors must be sent as data events rather than HTTP responses, and managing asyncio cancellation when clients disconnect.

The existing code shows good patterns in some areas (Pydantic ValidationError handling, specific HTTPExceptions) but falls back to broad exception catching in 19 locations. These handlers primarily exist in:
- **openai.py (8 handlers)**: Heavy streaming logic with broad catches
- **indexer.py (5 handlers)**: File operations and task management
- **tools.py (2 handlers)**: Tool execution with cleanup logic
- **utils.py (1 handler)**: Model availability checking
- **actors.py (2 handlers)**: Ray actor management
- **extract.py (1 handler)**: Document extraction

**Primary recommendation:** Replace bare exception handlers with a tiered catch strategy: specific exceptions first (OpenRAGError subclasses, Pydantic ValidationError, Ray errors), then generic Exception as a safety net that wraps unknown errors in a structured format without exposing internals.

## Standard Stack

### Core Exception Framework

| Library/Component | Purpose | Current State |
|-------------------|---------|---------------|
| **OpenRAGError** | Base exception class | ✅ Already implemented in `utils/exceptions/base.py` |
| **VDBError subclasses** | Vector database errors | ✅ 10 specific types in `utils/exceptions/vectordb.py` |
| **EmbeddingError subclasses** | Embedding API errors | ✅ 3 specific types in `utils/exceptions/embeddings.py` |
| **FastAPI HTTPException** | HTTP-level errors | ✅ Already used throughout routers |
| **Pydantic ValidationError** | Request validation | ✅ Handled in `utils.py:validate_metadata` |
| **Ray exceptions** | Distributed task errors | ✅ `RayTaskError`, `TaskCancelledError` imported in `ray_utils.py` |

### Streaming Error Handling

| Component | Purpose | Notes |
|-----------|---------|-------|
| **StreamingResponse** | FastAPI SSE streaming | Already used in `openai.py` |
| **asyncio.CancelledError** | Client disconnection | Must be caught in streaming generators |
| **SSE error event format** | Error messages in stream | Custom JSON structure with error fields |

**Installation:** No additional packages required - all exception types already exist in the codebase.

## Architecture Patterns

### Current Exception Hierarchy

```
Exception
└── OpenRAGError (base.py)
    ├── message: str
    ├── code: str
    ├── status_code: int
    ├── extra: dict
    └── to_dict() → {"detail": "[code]: message", "extra": {...}}

    ├── VDBError (vectordb.py)
    │   ├── VDBConnectionError (503)
    │   ├── VDBCreateOrLoadCollectionError (422)
    │   ├── VDBInsertError (422)
    │   ├── VDBFileIDAlreadyExistsError (409)
    │   ├── VDBDeleteError (422)
    │   ├── VDBSearchError (422)
    │   ├── VDBPartitionNotFound (404)
    │   ├── VDBFileNotFoundError (404)
    │   ├── VDBUserNotFound (404)
    │   ├── VDBMembershipNotFound (404)
    │   └── UnexpectedVDBError (500)

    └── EmbeddingError (embeddings.py)
        ├── EmbeddingAPIError (500)
        ├── EmbeddingResponseError (422)
        └── UnexpectedEmbeddingError (500)
```

### Global Exception Handler (Already Exists)

Location: `api.py` lines 163-167

```python
@app.exception_handler(OpenRAGError)
async def openrag_exception_handler(request: Request, exc: OpenRAGError):
    logger = get_logger()
    logger.error("OpenRAGError occurred", error=str(exc))
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
```

**Key insight:** This handler already exists and converts OpenRAGError to structured JSON. Routers just need to raise specific OpenRAGError subclasses instead of catching broad exceptions.

### Pattern 1: Non-Streaming Endpoint Error Handling

**What:** Tiered exception handling for regular HTTP endpoints
**When to use:** All non-streaming routes

**Example - Current pattern (BAD):**
```python
# openai.py line 173
try:
    partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
except Exception as e:
    log.warning("Invalid model or partition", error=str(e))
    raise
```

**Recommended pattern (GOOD):**
```python
try:
    partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
except HTTPException:
    # Re-raise FastAPI exceptions unchanged
    raise
except VDBPartitionNotFound as e:
    # Let the global handler convert to JSON
    raise
except Exception as e:
    # Wrap unknown errors without exposing internals
    log.exception("Unexpected error getting partition", error=str(e))
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred while processing the request"
    )
```

### Pattern 2: Streaming Response Error Handling

**What:** Errors during SSE streaming must be sent as data events, not HTTP responses
**When to use:** StreamingResponse with async generators

**Current pattern (openai.py line 208):**
```python
async def stream_response():
    try:
        async for line in llm_output:
            # ... process line ...
    except Exception as e:
        log.warning("Error while generating streaming answer", error=str(e))
        error_chunk = {
            "error": {
                "message": f"Error while generating answer: {str(e)}",
                "type": "error",
                "param": None,
                "code": "ERROR_ANSWER_GENERATION",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
```

**Analysis:** This pattern is GOOD but needs to:
1. Catch specific exceptions first (OpenRAGError, Ray errors)
2. Handle asyncio.CancelledError separately (client disconnect)
3. Avoid exposing error details from generic Exception

**Recommended enhanced pattern:**
```python
async def stream_response():
    try:
        async for line in llm_output:
            # ... process line ...
    except asyncio.CancelledError:
        # Client disconnected - clean shutdown, no error event
        log.info("Client disconnected from stream")
        return
    except (RayTaskError, TaskCancelledError) as e:
        log.warning("Ray task error in stream", error=str(e))
        error_chunk = {
            "error": {
                "message": "Processing task was cancelled or failed",
                "type": "task_error",
                "code": "RAY_TASK_ERROR"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
    except OpenRAGError as e:
        log.warning("OpenRAG error in stream", error=str(e))
        error_chunk = {
            "error": {
                "message": e.message,
                "type": "openrag_error",
                "code": e.code
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        log.exception("Unexpected error in stream")
        error_chunk = {
            "error": {
                "message": "An unexpected error occurred during streaming",
                "type": "error",
                "code": "STREAM_ERROR"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
```

### Pattern 3: Ray Actor Call Error Handling

**What:** Specific handling for Ray remote actor calls
**When to use:** Any `await actor.method.remote()` call

**Existing utility (ray_utils.py):**
```python
async def call_ray_actor_with_timeout(
    future: ray.ObjectRef,
    timeout: float,
    task_description: str = "Ray task",
) -> Any:
    """Handles timeout, cancellation, and Ray-specific exceptions"""
    try:
        result = await asyncio.wait_for(asyncio.gather(future), timeout=timeout)
        return result[0]
    except TimeoutError:
        logger.warning(f"{task_description} timed out, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise
    except asyncio.CancelledError:
        logger.warning(f"{task_description} cancelled, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise
    except TaskCancelledError:
        logger.warning(f"{task_description} Ray task was cancelled")
        raise
    except RayTaskError as e:
        raise RuntimeError(f"{task_description} failed") from e
```

**Recommendation:** Use this utility where appropriate, but most router calls don't need timeout handling. For simple cases, just catch RayTaskError directly.

### Pattern 4: Cleanup with Finally Blocks

**What:** File cleanup in tools.py (lines 124-141) uses try/except/finally correctly
**When to use:** Resource cleanup that must happen even on error

**Current pattern (GOOD):**
```python
try:
    # ... file processing ...
except HTTPException:
    raise
except Exception as e:
    logger.exception("Failed during tool execution.")
    raise HTTPException(...)
finally:
    # Cleanup temporary file
    if file_path is not None:
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as cleanup_err:
            logger.warning("Failed to delete temporary file.")
```

**Recommendation:** Keep this pattern. The nested try/except in finally is appropriate since cleanup failures shouldn't block the response.

### Anti-Patterns to Avoid

- **Silent failures:** Never `except Exception: pass` - always log and raise/return error
- **Exposing internals:** Generic exception messages should not include stack traces or internal paths
- **Catching HTTPException:** Don't catch and re-wrap HTTPException - let FastAPI handle it
- **Ignoring asyncio.CancelledError:** Must propagate or log cleanly for streaming endpoints

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Custom exception base classes | New hierarchy | Extend existing `OpenRAGError` | Already has code, message, status_code, to_dict() |
| SSE error format | Custom protocol | OpenAI-compatible error chunks | Already used in codebase |
| Ray error conversion | Custom wrappers | `RayTaskError`, `TaskCancelledError` | Ray provides these |
| Request validation | Manual checks | Pydantic ValidationError | Already handled in `validate_metadata` |
| Global error handler | Multiple handlers | Single `@app.exception_handler(OpenRAGError)` | Already exists in api.py |

**Key insight:** The exception infrastructure is already built. This phase is about *using* it consistently, not creating new patterns.

## Common Pitfalls

### Pitfall 1: Re-wrapping HTTPException

**What goes wrong:** Catching HTTPException and wrapping in another HTTPException loses status code
**Why it happens:** Overly broad exception handlers catch FastAPI's own exceptions
**How to avoid:** Always re-raise HTTPException unchanged
**Warning signs:** Tests fail with wrong status codes (500 instead of 404, 400, etc.)

**Example:**
```python
# BAD
try:
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(status_code=404, detail="File not found")
except Exception:  # This catches the HTTPException too!
    raise HTTPException(status_code=500, detail="Internal error")

# GOOD
try:
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(status_code=404, detail="File not found")
except HTTPException:
    raise  # Preserve the original
except Exception:
    raise HTTPException(status_code=500, detail="Internal error")
```

### Pitfall 2: Streaming Response After Headers Sent

**What goes wrong:** Once StreamingResponse starts yielding, you can't change HTTP status codes
**Why it happens:** Trying to raise HTTPException from within the generator
**How to avoid:** Use SSE error events (data frames) instead of HTTP exceptions
**Warning signs:** Errors logged but client receives incomplete stream with no error indication

**Example:**
```python
# BAD
async def stream_response():
    async for chunk in data:
        yield chunk
        if error_condition:
            raise HTTPException(...)  # Headers already sent!

# GOOD
async def stream_response():
    async for chunk in data:
        yield chunk
        if error_condition:
            error_event = {"error": {"message": "...", "code": "..."}}
            yield f"data: {json.dumps(error_event)}\n\n"
            yield "data: [DONE]\n\n"
            return
```

### Pitfall 3: Not Propagating asyncio.CancelledError

**What goes wrong:** Client disconnects but server keeps processing, wasting resources
**Why it happens:** Catching Exception which includes CancelledError in Python 3.12+
**How to avoid:** Catch CancelledError first and re-raise or return cleanly
**Warning signs:** Server CPU usage stays high after client disconnects, memory leaks

**Example:**
```python
# BAD
async def stream_response():
    try:
        async for chunk in data:
            await asyncio.sleep(0)  # Cancellation point
            yield chunk
    except Exception:  # Catches CancelledError!
        yield "error"

# GOOD
async def stream_response():
    try:
        async for chunk in data:
            await asyncio.sleep(0)
            yield chunk
    except asyncio.CancelledError:
        log.info("Client disconnected")
        return  # Clean shutdown
    except Exception:
        yield "error"
```

### Pitfall 4: Exposing Internal Error Details

**What goes wrong:** Generic exception messages leak file paths, database schemas, or stack traces to clients
**Why it happens:** Using `str(e)` or `repr(e)` directly in error responses
**How to avoid:** Generic "unexpected error" message for Exception catch-all, log details server-side
**Warning signs:** Error messages contain "/app/openrag/...", SQL queries, or Python stack frames

**Example:**
```python
# BAD
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))  # May expose internals

# GOOD
except Exception as e:
    log.exception("Unexpected error during file upload", file_id=file_id)
    raise HTTPException(
        status_code=500,
        detail="An unexpected error occurred during file upload"
    )
```

## Code Examples

Verified patterns from the OpenRAG codebase:

### Example 1: Basic Tiered Exception Handling

```python
# Source: openrag/routers/indexer.py (to be fixed)
# Current (line 145):
try:
    file_path = await save_file_to_disk(file, save_dir, with_random_prefix=True)
except Exception as e:
    log.exception("Failed to save file to disk.", error=str(e))
    raise HTTPException(status_code=500, detail=str(e))

# Recommended fix:
try:
    file_path = await save_file_to_disk(file, save_dir, with_random_prefix=True)
except OSError as e:
    # Specific disk/permission errors
    log.exception("Failed to save file to disk", error=str(e))
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to save uploaded file due to storage error"
    )
except Exception as e:
    # Unknown errors
    log.exception("Unexpected error saving file", error=str(e))
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred while saving the file"
    )
```

### Example 2: Ray Actor Error Handling

```python
# Source: openrag/routers/actors.py (current good pattern at line 117)
try:
    actor = ray.get_actor(actor_name, namespace="openrag")
    ray.kill(actor, no_restart=True)
except ValueError:
    # Specific Ray error - actor not found
    logger.warning("Actor not found. Creating new instance.", actor=actor_name)
except Exception as e:
    logger.exception("Failed to kill actor", actor=actor_name)
    raise HTTPException(status_code=500, detail=f"Failed to kill actor {actor_name}: {e!s}")
```

### Example 3: Pydantic Validation Error (Already Correct)

```python
# Source: openrag/routers/utils.py lines 212-218 (keep this pattern)
try:
    validated = FileMetadataSchema(**parsed)
    return validated.model_dump()
except ValidationError as e:
    # Format Pydantic validation errors for user-friendly response
    errors = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Invalid metadata: {errors}"
    )
```

### Example 4: Streaming with Error Events

```python
# Source: openrag/routers/openai.py lines 192-220 (to be enhanced)
async def stream_response():
    try:
        async for line in llm_output:
            if line.startswith("data:"):
                if "[DONE]" in line:
                    yield f"{line}\n\n"
                else:
                    try:
                        data = json.loads(line[len("data: "):])
                        data["model"] = model_name
                        yield f"data: {json.dumps(data)}\n\n"
                    except json.JSONDecodeError as e:
                        log.error("Failed to decode streamed chunk.", error=str(e))
                        raise
    except asyncio.CancelledError:
        log.info("Client disconnected from stream")
        return
    except Exception as e:
        log.warning("Error while generating streaming answer", error=str(e))
        error_chunk = {
            "error": {
                "message": "An error occurred during streaming",
                "type": "error",
                "code": "STREAM_ERROR"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Bare `except Exception:` everywhere | Tiered specific exception catching | FastAPI 0.100+ (2023) | Better error diagnosis, structured responses |
| HTTP status in streaming generators | SSE error data events | SSE spec adoption | Proper error signaling after headers sent |
| BaseException catching | Separate asyncio.CancelledError handling | Python 3.8+ | Proper cancellation propagation |
| Generic error messages | Structured error objects (RFC 9457) | 2024-2025 | Client-friendly error parsing |
| Custom exception base classes per module | Single inheritance hierarchy | Modern Python patterns | Consistent error handling |

**Deprecated/outdated:**
- **BaseException catching:** In Python 3.8+, CancelledError moved from BaseException to Exception. Must catch it separately.
- **SSE libraries (sse-starlette):** Not needed - FastAPI's StreamingResponse with custom error events is sufficient for OpenRAG's use case.

## Inventory of Exception Handlers

### Complete list of 19 bare exception handlers:

1. **openai.py:173** - get_partition_name error (re-raises)
2. **openai.py:180** - ragpipe.chat_completion error → HTTPException 500
3. **openai.py:208** - streaming generator error → SSE error event
4. **openai.py:229** - non-streaming response error → HTTPException 500
5. **openai.py:296** - get_partition_name error (re-raises)
6. **openai.py:303** - ragpipe.completions error → HTTPException 500
7. **openai.py:318** - non-streaming response error → HTTPException 500
8. **indexer.py:145** - save_file_to_disk error → HTTPException 500
9. **indexer.py:260** - save_file_to_disk error → HTTPException 500
10. **indexer.py:537** - task error retrieval → HTTPException 500
11. **indexer.py:585** - task logs fetch → HTTPException 500
12. **indexer.py:618** - task cancellation → HTTPException 500
13. **tools.py:124** - tool execution error → HTTPException 500
14. **tools.py:137** - cleanup error in finally block (logs only)
15. **utils.py:267** - LLM model availability check → HTTPException 500
16. **actors.py:66** - list_actors error → HTTPException 500
17. **actors.py:119** - kill actor error → HTTPException 500
18. **actors.py:139** - restart actor error → HTTPException 500
19. **extract.py:70** - get_chunk_by_id error → HTTPException 500

### Exception Types to Catch Specifically

Based on operations in handlers:

| Operation | Specific Exceptions | Fallback |
|-----------|-------------------|----------|
| Ray actor calls | `RayTaskError`, `TaskCancelledError`, `ValueError` (actor not found) | Generic RuntimeError wrapper |
| File I/O | `OSError`, `IOError`, `PermissionError` | HTTPException 500 |
| JSON parsing | `json.JSONDecodeError` | HTTPException 500 |
| Vector DB ops | `VDBError` subclasses | HTTPException 500 |
| Pydantic validation | `ValidationError` | HTTPException 400 |
| LLM API calls | `OpenAIError`, `httpx.HTTPError` | HTTPException 503 or 500 |
| Streaming | `asyncio.CancelledError` | SSE error event |

## Open Questions

1. **Should we add new OpenRAGError subclasses for specific router operations?**
   - What we know: Current hierarchy covers VDB and Embedding errors
   - What's unclear: Whether file I/O, serialization, or task management need their own error types
   - Recommendation: Start with existing types + standard Python exceptions (OSError, etc.). Add new OpenRAGError subclasses only if patterns emerge requiring domain-specific error codes

2. **How should we handle errors from the LLM/VLM external APIs?**
   - What we know: openai.py makes external API calls that can fail
   - What's unclear: Whether to catch openai.OpenAIError specifically or treat as generic API error
   - Recommendation: Catch httpx errors and openai library errors, map to 503 (service unavailable) since it's an external dependency

3. **Should streaming endpoints support mid-stream recovery?**
   - What we know: Current pattern sends error event and terminates stream
   - What's unclear: Whether to attempt retry logic or just fail fast
   - Recommendation: Fail fast with clear error event. Retries should happen at client level since stream state is already corrupted

## Sources

### Primary (HIGH confidence)

- **OpenRAG codebase:**
  - `/openrag/utils/exceptions/base.py` - OpenRAGError base class
  - `/openrag/utils/exceptions/vectordb.py` - 10 VDB exception types
  - `/openrag/utils/exceptions/embeddings.py` - 3 embedding exception types
  - `/openrag/api.py` - Global exception handler (lines 163-167)
  - `/openrag/components/ray_utils.py` - Ray error handling utility
  - `/openrag/routers/*.py` - 19 exception handlers inventoried

- **Official FastAPI documentation:**
  - [Handling Errors - FastAPI](https://fastapi.tiangolo.com/tutorial/handling-errors/) - Exception handler patterns

### Secondary (MEDIUM confidence)

- [FastAPI Error Handling Patterns | Better Stack Community](https://betterstack.com/community/guides/scaling-python/error-handling-fastapi/) - Tiered exception catching
- [How to Handle Exceptions Globally in FastAPI](https://oneuptime.com/blog/post/2026-02-02-fastapi-global-exception-handling/view) - 2026 best practices
- [Implementing Server-Sent Events (SSE) with FastAPI](https://mahdijafaridev.medium.com/implementing-server-sent-events-sse-with-fastapi-real-time-updates-made-simple-6492f8bfc154) - SSE error handling
- [Streaming Response In FastAPI](https://medium.com/@ab.hassanein/streaming-responses-in-fastapi-d6a3397a4b7b) - Error handling in streams

### Tertiary (LOW confidence)

- [FastAPI StreamingResponse asyncio.CancelledError discussion](https://github.com/fastapi/fastapi/discussions/8673) - Community patterns for client disconnection
- [Railway discussion on CancelledError](https://station.railway.com/questions/getting-cancelled-error-python-fast-api-ap-76bfaaac) - Real-world streaming error scenarios

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All exception types already exist in codebase, verified by reading source files
- Architecture patterns: HIGH - Based on actual code review of api.py and router files
- Pitfalls: MEDIUM-HIGH - Based on common FastAPI patterns and codebase analysis, some inferred from code patterns
- Streaming error handling: MEDIUM - Based on web search and existing code in openai.py

**Research date:** 2026-02-10
**Valid until:** 2026-03-10 (30 days - stable domain, well-established FastAPI patterns)

**Router file analysis:**
- Total lines in routers: 2,526
- Bare exception handlers: 19
- Files with handlers: 7 (openai.py, indexer.py, tools.py, utils.py, actors.py, extract.py)
- Streaming endpoints: 2 (chat/completions, completions - only chat supports streaming)
- Ray actor calls: Extensive use throughout, particularly in actors.py and indexer.py
