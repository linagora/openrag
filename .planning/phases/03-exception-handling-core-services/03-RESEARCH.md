# Phase 3: Exception Handling - Core Services - Research

**Researched:** 2026-02-10
**Domain:** Python exception handling in Ray actors, vector database operations, and document processing pipelines
**Confidence:** HIGH

## Summary

Phase 3 targets 57 bare `except Exception` handlers across core service components: vectordb operations (13), indexer actor (5), loaders (19), embeddings (3), chunking (2), pipeline/retriever (5), and utilities (10). These handlers must be replaced with specific exception types while maintaining Ray actor exception propagation semantics and preserving all 93 existing tests.

The primary challenge is distinguishing between recoverable operational errors (network timeouts, VLM API failures) that should be logged and gracefully handled, versus unrecoverable errors (data corruption, programming bugs) that should propagate to callers. Ray actor exception propagation adds complexity: exceptions raised in remote methods are wrapped in RayTaskError when retrieved via ray.get() or asyncio.gather().

**Primary recommendation:** Replace broad handlers systematically by operation type (DB operations, embeddings, file I/O, LLM calls), using the existing exception hierarchy (VDBError, EmbeddingError) and Python standard exceptions (OSError for file I/O, httpx exceptions for HTTP), while preserving cleanup logic in finally blocks.

## Standard Stack

### Core Exception Types

| Exception Type | Source | Purpose | When to Catch |
|----------------|--------|---------|---------------|
| `VDBError` subclasses | `utils/exceptions/vectordb.py` | Vector DB operations | Milvus client calls, PostgreSQL operations |
| `EmbeddingError` subclasses | `utils/exceptions/embeddings.py` | Embedding generation | VLLM API calls in OpenAIEmbedding |
| `MilvusException` | `pymilvus` | Milvus client errors | Collection operations, search, insert |
| `openai.APIError` | `openai` SDK | OpenAI API base error | VLM captioning, LLM generation |
| `openai.APITimeoutError` | `openai` SDK | API timeout | Long VLM operations (already caught in chunker.py:71) |
| `openai.BadRequestError` | `openai` SDK | 400 errors from VLM | Invalid image data (already caught in base.py:141) |
| `httpx.HTTPStatusError` | `httpx` | HTTP 4xx/5xx responses | LLM API calls in llm.py |
| `httpx.RequestError` | `httpx` | Network/connection errors | LLM streaming operations |
| `RayTaskError` | `ray.exceptions` | Wrapped remote exceptions | Retrieving Ray actor results |
| `TaskCancelledError` | `ray.exceptions` | Ray task cancellation | Ray task was cancelled |
| `asyncio.CancelledError` | `asyncio` | Asyncio cancellation | Streaming operations (must catch FIRST) |
| `OSError` | Python stdlib | File I/O errors | File operations (includes FileNotFoundError, PermissionError) |

### OpenRAG Custom Exceptions

Already defined in `utils/exceptions/`:

**VDBError subclasses:**
- `VDBConnectionError` - Milvus/PostgreSQL connection failures
- `VDBCreateOrLoadCollectionError` - Collection operations
- `VDBInsertError` - Data insertion failures
- `VDBFileIDAlreadyExistsError` - Duplicate file detection (409 status)
- `VDBDeleteError` - Deletion failures
- `VDBSearchError` - Search operation failures
- `VDBPartitionNotFound` - Partition doesn't exist (404 status)
- `VDBFileNotFoundError` - File not found (404 status)
- `VDBUserNotFound` - User not found (404 status)
- `VDBMembershipNotFound` - Membership not found (404 status)
- `UnexpectedVDBError` - Catch-all for unexpected DB errors (500 status)

**EmbeddingError subclasses:**
- `EmbeddingAPIError` - API communication errors
- `EmbeddingResponseError` - Invalid/unexpected response format (422 status)
- `UnexpectedEmbeddingError` - Catch-all for unexpected embedding errors (500 status)

All OpenRAG exceptions inherit from `OpenRAGError` with `message`, `code`, `status_code`, and `extra` fields.

### External Library Exceptions

**pymilvus exceptions** (from [GitHub source](https://github.com/milvus-io/pymilvus/blob/master/pymilvus/exceptions.py)):
- `MilvusException` (base class with code, message)
- `ParamError` - Invalid parameters
- `ConnectError` - Connection failures
- `CollectionNotExistException` - Collection doesn't exist
- `PartitionAlreadyExistException` - Partition already exists

**OpenAI SDK exceptions** (from [OpenAI docs](https://platform.openai.com/docs/guides/error-codes/python-library-error-types)):
- `APIError` (base) - All API errors inherit from this
- `APIConnectionError` - Network failure/timeout
- `APITimeoutError` - Request timeout
- `RateLimitError` - Rate limit exceeded
- `AuthenticationError` - Invalid API key
- `BadRequestError` - 400 errors (invalid parameters)
- `InvalidRequestError` - Malformed request

**httpx exceptions** (from [HTTPX docs](https://www.python-httpx.org/exceptions/)):
- `HTTPError` (base) - All httpx exceptions inherit from this
- `RequestError` (subclass) - Request failed before response (network, timeout)
- `HTTPStatusError` (subclass) - 4xx/5xx status codes (raised by response.raise_for_status())

**Ray exceptions** (from [Ray docs](https://docs.ray.io/en/latest/ray-core/api/exceptions.html)):
- `RayTaskError` - Task threw exception during execution (wraps original exception)
- `RayActorError` - Actor unreachable/dead
- `ActorDiedError` (subclass of RayActorError) - Actor died during task execution
- `TaskCancelledError` - Task was cancelled

## Architecture Patterns

### Pattern 1: Exception Inventory by Component

**Vectordb (13 handlers in vectordb.py):**
- Line 178: MilvusDB.__init__() - catch VDBError, wrap others in VDBConnectionError
- Line 244: load_collection() - catch VDBError, wrap others in UnexpectedVDBError
- Line 413: async_add_documents() - catch EmbeddingError, VDBError separately, wrap others
- Lines 585, 659, 717, 761, 774, 791, 805, 838, 853, 917: Various Milvus operations

**Indexer (5 handlers in indexer.py):**
- Line 122: add_file() - top-level catch-all (logs, updates task state, re-raises)
- Line 139: add_file() finally block cleanup - file deletion errors only
- Lines 160, 187, 219: delete_file(), update_file_metadata(), copy_file()

**Loaders (19 handlers across multiple files):**
- base.py:119 - base64 decode errors during image captioning
- base.py:146 - external resource errors during VLM captioning
- eml_loader.py - 10 handlers for email parsing (MIME parts, attachments, encoding)
- image.py:32 - image loading errors
- media_loader.py - 3 handlers for audio/video transcription
- pptx_loader.py:131 - PPTX slide extraction
- serializer.py:87 - document serialization catch-all
- pdf_loaders/*.py - 5 handlers for PDF processing

**Embeddings (3 handlers in openai.py):**
- Line 26: embedding_dimension property - re-raise all
- Line 62: embed_documents() - catch openai.APIError, IndexError/AttributeError, wrap others
- Line 78: embed_query() - re-raise all (calls embed_documents)

**Chunker (2 handlers in chunker.py):**
- Line 77: _generate_context() - catch openai.APITimeoutError separately, log others
- Line 133: contextualize_chunks() - top-level catch-all (returns original chunks on error)

**Pipeline/Retriever (5 handlers):**
- pipeline.py:198, 210 - completions() and chat_completion() catch-all (logs and re-raises)
- map_reduce.py:82 - infer_chunk_relevancy() catch-all (returns irrelevant chunk)
- reranker.py:40 - rerank() catch-all (logs and re-raises)
- llm.py:69 - streaming error catch-all (logs and re-raises)

**Utils (10 handlers):**
- utils.py:65 - semaphore initialization
- vectordb/utils.py - 4 handlers in PartitionFileManager (DB operations)

### Pattern 2: Ray Actor Exception Propagation

Ray actors wrap exceptions in `RayTaskError` when results are retrieved:

```python
# In vectordb actor method
async def async_search(self, query: str, ...):
    try:
        # Milvus operations
        results = await self._async_client.search(...)
    except MilvusException as e:
        raise VDBSearchError(f"Search failed: {e}")

# In router/pipeline calling the actor
vectordb = ray.get_actor("Vectordb", namespace="openrag")
try:
    results = await vectordb.async_search.remote(query=query)
except RayTaskError as e:
    # Original VDBSearchError is wrapped inside
    # Can access via e.cause or let it propagate
    raise
```

**Key insight:** Routers catch `RayTaskError` and convert to HTTP responses. Components should raise typed exceptions knowing they'll be wrapped. The `call_ray_actor_with_timeout` utility (components/ray_utils.py:11-58) already handles Ray exception patterns correctly.

### Pattern 3: Exception Handler Replacement Strategy

Based on Phase 2 decisions (from additional_context):

1. **Catch specific exceptions FIRST, broadest LAST:**
   ```python
   try:
       # operation
   except asyncio.CancelledError:  # FIRST for streaming
       raise
   except openai.APITimeoutError:
       logger.warning("Timeout")
       return default_value
   except openai.BadRequestError as e:
       logger.warning("Bad request", error=str(e))
       return default_value
   except openai.APIError as e:
       logger.error("API error", error=str(e))
       raise CustomError(f"Failed: {e}")
   except Exception as e:  # LAST - generic message
       logger.exception("Unexpected error")
       raise CustomError("An unexpected error occurred")
   ```

2. **Preserve cleanup logic in finally blocks:**
   ```python
   try:
       # operation
   except SpecificError as e:
       # handle
       raise
   finally:
       try:
           # cleanup that might fail
           Path(temp_file).unlink(missing_ok=True)
       except Exception as cleanup_err:
           logger.warning("Cleanup failed", error=str(cleanup_err))
   ```

3. **OSError is the base for file I/O errors:**
   ```python
   try:
       with open(path) as f:
           data = f.read()
   except OSError as e:  # Catches FileNotFoundError, PermissionError, etc.
       logger.error("File operation failed", path=path, error=str(e))
       raise CustomError(f"Cannot read file: {e}")
   ```

4. **Generic error messages for Exception catch-all:**
   Never expose internal details in generic handlers:
   ```python
   except Exception as e:
       logger.exception("Unexpected error in operation")
       raise CustomError("An unexpected error occurred")  # Generic message
   ```

### Pattern 4: Operation-Specific Exception Handling

**Milvus operations:**
```python
try:
    result = self._client.search(collection_name=self.collection_name, ...)
except MilvusException as e:
    raise VDBSearchError(f"Search failed: {e}", collection_name=self.collection_name)
```

**Embedding operations:**
```python
try:
    response = self._sync_client.embeddings.create(model=self.embedding_model, input=texts)
    return [vector.embedding for vector in response.data]
except openai.APIError as e:
    raise EmbeddingAPIError(f"API error: {e}", model_name=self.embedding_model)
except (IndexError, AttributeError) as e:
    raise EmbeddingResponseError("Invalid response format", error=str(e))
```

**PostgreSQL operations:**
```python
try:
    with self.Session() as session:
        result = session.query(File).filter(...).first()
except Exception as e:
    raise VDBConnectionError(f"Database query failed: {e}")
```

**LLM/VLM calls:**
```python
try:
    response = await self.vlm_endpoint.ainvoke([message])
except openai.APITimeoutError:
    logger.warning("VLM timeout")
    return ""  # Graceful degradation
except openai.BadRequestError as e:
    logger.warning("VLM rejected request", error=str(e)[:300])
    return ""  # Expected failure for invalid images
except openai.APIError as e:
    logger.error("VLM API error", error=str(e))
    raise
```

### Anti-Patterns to Avoid

- **Catching Exception without re-raising or converting:** Every `except Exception` should either raise a typed exception or have explicit justification for suppression
- **Exposing internal details in error messages:** Generic catch-alls must use generic messages
- **Not preserving asyncio.CancelledError:** Must catch and re-raise first in streaming operations
- **Forgetting Ray wraps exceptions:** Components should raise typed exceptions knowing routers will catch RayTaskError

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Ray actor timeout and cancellation | Manual ray.wait() with timeout loops | `call_ray_actor_with_timeout()` from ray_utils.py | Already handles timeout, cancellation, RayTaskError, TaskCancelledError correctly |
| Exception logging context | String formatting in every handler | Loguru's .bind() for structured context | Already used throughout codebase (file_id, partition, task_id) |
| HTTP exception handling in LLM calls | Custom retry/error logic | httpx.HTTPStatusError and httpx.RequestError | Standard library provides correct HTTP semantics |
| File I/O exception handling | Catching FileNotFoundError, PermissionError separately | Catch OSError (base class) | OSError covers all file I/O errors per Phase 2 decision |

**Key insight:** The codebase already has the right primitives (call_ray_actor_with_timeout, OpenRAGError hierarchy, structured logging). The work is surgical replacement, not architectural change.

## Common Pitfalls

### Pitfall 1: Breaking Ray Actor Exception Semantics

**What goes wrong:** Catching RayTaskError in component code instead of letting it propagate to routers.

**Why it happens:** Components are Ray actors, but exception handling patterns look like regular Python code.

**How to avoid:**
- Component methods should raise typed exceptions (VDBError, EmbeddingError)
- Routers catch RayTaskError and convert to HTTP responses
- Use call_ray_actor_with_timeout() which already handles Ray exceptions correctly

**Warning signs:** Seeing `try: await actor.method.remote() except Exception:` in component code.

### Pitfall 2: Suppressing Errors Without Justification

**What goes wrong:** Silent failures where operations fail but execution continues without logging or alerting.

**Why it happens:** Existing handlers use bare `except Exception: pass` or `except Exception: return default_value` without explanation.

**How to avoid:**
- Every exception handler must either re-raise (typed or generic), return early with logging, or have explicit comment justifying suppression
- Graceful degradation (returning empty string for VLM failures) is valid but must be logged
- File cleanup failures in finally blocks are valid to suppress but should warn

**Warning signs:** `except Exception: pass` without comment, handlers that return None/empty values without logging.

### Pitfall 3: Not Catching asyncio.CancelledError First

**What goes wrong:** Streaming operations don't properly handle client disconnection, leading to resource leaks.

**Why it happens:** asyncio.CancelledError is a BaseException subclass in Python 3.8+, but some handlers catch Exception first.

**How to avoid:** Always catch asyncio.CancelledError as the first exception handler in streaming operations, then re-raise.

**Warning signs:** Already correctly handled in 6 places (routers/openai.py:237, components/ray_utils.py:47, components/indexer/loaders/base.py:182, 251, pdf_loaders/docling2.py:131).

### Pitfall 4: Breaking Test Mocks

**What goes wrong:** Tests fail because mocked components return/raise different exception types than real implementations.

**Why it happens:** Tests may mock entire components without knowing internal exception types.

**How to avoid:**
- Check test files for mocks before changing component exceptions
- Ensure mocked methods raise same exception types as real implementations
- Run full test suite after each component change

**Warning signs:** Tests that pass before changes but fail after, especially integration tests that mock Ray actors.

### Pitfall 5: PostgreSQL vs Milvus Exception Confusion

**What goes wrong:** Using VDBConnectionError for all database errors when some are Milvus-specific vs PostgreSQL-specific.

**Why it happens:** PartitionFileManager uses SQLAlchemy (PostgreSQL), MilvusDB uses pymilvus client, but both use VDBError hierarchy.

**How to avoid:**
- VDBConnectionError is correct for both (connection failures)
- SQLAlchemy exceptions should be wrapped in appropriate VDBError subclasses
- Check if operation is metadata (PostgreSQL) or vector data (Milvus) before choosing exception

**Warning signs:** File metadata operations raising Milvus-related errors, or vice versa.

## Code Examples

Verified patterns from codebase analysis:

### Example 1: Milvus Operation with Typed Exceptions

```python
# From vectordb.py - GOOD pattern for Milvus operations
try:
    results = await self._async_client.search(
        collection_name=self.collection_name,
        data=query_vector,
        ...
    )
except MilvusException as e:
    self.logger.exception("Search failed", error=str(e))
    raise VDBSearchError(
        f"Failed to search collection: {e}",
        collection_name=self.collection_name,
    )
```

### Example 2: Embedding with Multiple Exception Types

```python
# From embeddings/openai.py - GOOD pattern showing progression
try:
    response = self._sync_client.embeddings.create(
        model=self.embedding_model,
        input=texts,
    )
    return [vector.embedding for vector in response.data]

except openai.APIError as e:
    logger.error("API error in embed_documents", error=str(e))
    raise EmbeddingAPIError(
        f"OpenAI API error during document embedding: {e}",
        model_name=self.embedding_model,
    )

except (IndexError, AttributeError) as e:
    logger.error("Error while accessing embedding data", error=str(e))
    raise EmbeddingResponseError(
        "Failed to retrieve document embeddings due to unexpected response format.",
        error=str(e),
    )

except Exception as e:
    logger.exception("Unexpected error while embedding documents", error=str(e))
    raise UnexpectedEmbeddingError(
        f"Failed to embed documents: {e}",
        model_name=self.embedding_model,
    )
```

### Example 3: VLM Captioning with Graceful Degradation

```python
# From loaders/base.py - GOOD pattern for VLM failures
try:
    response = await self.vlm_endpoint.ainvoke([message])
    image_description = response.content

except BadRequestError as e:
    # 400 errors are expected for invalid images
    logger.warning("VLM rejected image captioning request", error=str(e)[:300])
    image_description = ""

except Exception as e:
    is_external, status_code, url = is_external_resource_error(e)
    if is_external:
        # Expected failure for unreachable external URLs
        logger.warning("VLM cannot fetch external resource", status_code=status_code, url=url)
        image_description = ""
    else:
        # Unexpected failure - log but don't fail document processing
        logger.error("VLM captioning failed", error=str(e))
        image_description = ""
```

### Example 4: Cleanup with Nested Try-Except

```python
# From indexer.py - GOOD pattern for cleanup in finally
try:
    # Main operation
    doc = await serialize_file(path, metadata)
    chunks = await chunk(doc)
    await insert_documents(chunks)

except Exception as e:
    logger.exception("Task failed", task_id=task_id)
    raise

finally:
    # Resource cleanup
    if torch.cuda.is_available():
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()

    try:
        # File cleanup might fail if file doesn't exist or no permissions
        if not save_uploaded_files:
            Path(path).unlink(missing_ok=True)
    except Exception as cleanup_err:
        logger.warning("Failed to delete input file", path=path, error=str(cleanup_err))
```

### Example 5: LLM Streaming with HTTPx Exceptions

```python
# From llm.py - Replace bare Exception with specific types
async def chat_completion(self, request: dict):
    timeout = httpx.Timeout(4 * 60)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream:
            try:
                async with client.stream("POST", url=url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        error_detail = response.text
                        raise ValueError(f"LLM API error ({response.status_code}): {error_detail}")
                    async for line in response.aiter_lines():
                        yield line

            except ValueError:
                # Already raised above, don't wrap
                raise

            except httpx.RequestError as e:
                # Network/connection failures
                logger.error("Network error while streaming", error=str(e))
                raise

            except httpx.HTTPStatusError as e:
                # 4xx/5xx not caught by status_code check above
                logger.error("HTTP error while streaming", status_code=e.response.status_code)
                raise

            except Exception as e:
                # Truly unexpected errors
                logger.exception("Unexpected error while streaming")
                raise RuntimeError("An unexpected error occurred during streaming")
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Bare `except Exception` everywhere | Typed exceptions (VDBError, EmbeddingError) + generic catch-all | Phases 2-3 (2026) | Better observability, explicit error handling, safer API responses |
| ray.get() with blocking | asyncio.gather() with call_ray_actor_with_timeout() | Phase 4 (planned) | Proper async, cancellation support |
| String concatenation for DB URLs | SQLAlchemy URL.create() | Phase 1 (completed) | No SQL injection risk |
| No metadata validation | Pydantic schema validation | Phase 1 (completed) | Type safety, input validation |

**Deprecated/outdated:**
- Using bare `except Exception: pass` - NOW requires explicit justification comment
- Catching Exception without converting to typed exception - NOW must wrap in OpenRAGError subclass or re-raise
- Ignoring asyncio.CancelledError - NOW must catch first and re-raise in streaming operations

## Open Questions

1. **Should loaders suppress ALL VLM captioning errors or only specific ones?**
   - What we know: base.py:146 catches all exceptions after BadRequestError, logs, returns empty string (graceful degradation)
   - What's unclear: Should openai.APIError (rate limits, auth failures) propagate instead of being suppressed?
   - Recommendation: Keep current behavior (suppress all for graceful degradation), but add metric/counter for VLM failure rate

2. **Should PartitionFileManager wrap all SQLAlchemy exceptions in VDBError subclasses?**
   - What we know: 4 bare Exception handlers in utils.py, but SQLAlchemy has rich exception hierarchy
   - What's unclear: Do we need new VDBError subclasses for constraint violations, integrity errors?
   - Recommendation: Start with VDBConnectionError for connection/session errors, use existing VDBInsertError/VDBDeleteError for DML operations, add new subclass if needed

3. **Should eml_loader.py's 10 exception handlers be consolidated?**
   - What we know: Deeply nested try-except for MIME parsing, attachment extraction, encoding detection
   - What's unclear: Are all these handlers necessary or is there a simpler pattern?
   - Recommendation: Keep nested structure (email parsing is inherently fragile), but replace bare Exception with specific email.errors.* exceptions where possible

4. **How should test mocks handle new exception types?**
   - What we know: Only 4 test files in components/, unclear how extensively components are mocked
   - What's unclear: Do integration tests mock Ray actors? If so, do they need to return new exception types?
   - Recommendation: Run tests after each component change, update mocks only if tests fail

## Sources

### Primary (HIGH confidence)

- Codebase grep analysis - 57 `except Exception` instances counted across components
- `utils/exceptions/base.py`, `vectordb.py`, `embeddings.py` - OpenRAG exception hierarchy
- `components/ray_utils.py` - Ray exception handling utility (lines 11-58)
- Phase 2 decisions from additional_context - OSError for file I/O, asyncio.CancelledError first in streaming
- [Ray exceptions documentation](https://docs.ray.io/en/latest/ray-core/api/exceptions.html) - RayTaskError, RayActorError behavior
- [HTTPX exceptions documentation](https://www.python-httpx.org/exceptions/) - HTTPStatusError, RequestError hierarchy

### Secondary (MEDIUM confidence)

- [PyMilvus exceptions source](https://github.com/milvus-io/pymilvus/blob/master/pymilvus/exceptions.py) - MilvusException hierarchy
- [OpenAI Python SDK error types](https://platform.openai.com/docs/guides/error-codes/python-library-error-types) - APIError, APITimeoutError, BadRequestError
- Existing codebase patterns - VLM captioning graceful degradation (base.py:141-146), chunker timeout handling (chunker.py:71-83)

### Tertiary (LOW confidence)

- None - all findings verified against codebase or official documentation

## Metadata

**Confidence breakdown:**
- Exception inventory: HIGH - grep verified against actual files
- Exception hierarchy: HIGH - directly from codebase and official docs
- Ray semantics: HIGH - verified in ray_utils.py and Ray official docs
- Test impact: MEDIUM - limited visibility into test mocks, requires validation

**Research date:** 2026-02-10
**Valid until:** 2026-03-10 (30 days - stable technologies, unlikely to change)
