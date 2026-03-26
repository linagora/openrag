# File Attachments RAG Design

**Date:** 2026-03-25  
**Status:** Draft  
**Author:** OpenRAG Agent

## Overview

Add support for injecting specific file chunks via `metadata.attachments` in the `/chat/completions` endpoint. When file IDs are provided, the system skips semantic search and retrieves chunks directly from the specified files for answer generation.

## Problem Statement

Currently, OpenRAG only supports semantic search across partitions. Users cannot query specific documents they know about. This limits use cases like:
- Asking questions about a specific document in a conversation
- Referencing previously uploaded files without re-uploading
- Building workflows that target known document IDs

## Solution

Add an `attachments` field to the `metadata` parameter that accepts a list of file references. When present, the system retrieves chunks by file ID instead of performing semantic search.

## Attachments Format

```json
{
  "metadata": {
    "attachments": [
      {"id": "file_id_1"},
      {"id": "file_id_2"},
      {"id": "file_id_3"}
    ]
  }
}
```

**Attachment Schema:** Defined as a Pydantic model for validation:

```python
class Attachment(BaseModel):
    id: str = Field(..., min_length=1, description="File ID")
    type: Literal["file"] | None = Field(None, description="For future extensibility")
    priority: int | None = Field(None, ge=0, description="For future ranking")
```

**Validation Rules:**
- `id`: Required, non-empty string
- Invalid attachments (missing/empty `id`) are silently skipped
- Extra fields are ignored (forward compatible)

## Behavior

| Scenario | Behavior |
|----------|----------|
| `attachments` not provided | Normal semantic search flow |
| `attachments: []` (empty list) | Normal semantic search flow |
| All file_ids don't exist | Empty chunks → empty context → LLM responds without RAG |
| Some file_ids don't exist | Only valid chunks returned (logs warning) |
| Invalid attachment format | Silently skip invalid entries (missing/empty "id" field) |
| File_id not in specified partition | No chunks returned for that file (logs warning) |

**Chunk ordering:** Chunks are grouped by file_id and maintain the order specified in the attachments list. Within each file, chunks maintain their original order.

**Note:** Chunk limits will be added in v2. For now, all chunks are retrieved per file.

## Architecture

### Components Modified

1. **`openrag/models/openai.py`** - Add attachments to metadata default
2. **`openrag/components/indexer/vectordb/vectordb.py`** - Add `get_chunks_by_file_ids()` method
3. **`openrag/components/pipeline.py`** - Add conditional logic to bypass semantic search

### Data Flow

```
User Request with attachments
         ↓
RagPipeline._prepare_for_chat_completion()
         ↓
Extract file_ids from attachments
         ↓
Vectordb.get_chunks_by_file_ids()
         ↓
Chunks grouped by file_id (maintaining order)
         ↓
Format context (same as normal RAG)
         ↓
LLM generates response
```

## Implementation Details

### 1. Model Update (`openrag/models/openai.py`)

Add `Attachment` model and `MetadataDict` TypedDict:

```python
from typing import TypedDict

class Attachment(BaseModel):
    """Represents a file attachment for RAG retrieval."""
    id: str = Field(..., min_length=1, description="File ID")
    type: Literal["file"] | None = Field(None, description="For future extensibility")
    priority: int | None = Field(None, ge=0, description="For future ranking")


class MetadataDict(TypedDict, total=False):
    """TypedDict for metadata field with known keys."""
    use_map_reduce: bool
    spoken_style_answer: bool
    websearch: bool
    llm_override: dict[str, Any] | None
    attachments: list[dict[str, Any]] | None


class OpenAIChatCompletionRequest(BaseModel):
    metadata: MetadataDict | None = Field(
        default_factory=lambda: {
            "use_map_reduce": False,
            "spoken_style_answer": False,
            "websearch": False,
            "llm_override": None,
            "attachments": None,
        },
        description="...",
    )
```

**Type Safety:** `TypedDict` provides type hints for IDE autocomplete and static type checkers (mypy, pyright). Runtime validation still uses `Attachment.model_validate()` for attachment items.

### 2. Vectordb Method (`openrag/components/indexer/vectordb/vectordb.py`)

```python
import asyncio
from utils.exceptions.vectordb import VDBError

async def _retrieve_file_chunks(
    self,
    file_id: str,
    partition: list[str] | None,
    include_id: bool = True
) -> list[Document]:
    """Helper to retrieve chunks for a single file_id across partitions.
    
    Checks file existence before querying. Uses filter expression like async_search.
    """
    if not partition:
        return []
    
    # Check file existence in specified partitions
    file_found = False
    if partition == ["all"]:
        all_partitions = await self.list_partitions.remote()
        for p in all_partitions:
            if self.file_exists(file_id=file_id, partition=p["partition"]):
                file_found = True
                break
    else:
        for partition_name in partition:
            if self.file_exists(file_id=file_id, partition=partition_name):
                file_found = True
                break
    
    if not file_found:
        self.logger.warning("File not found in specified partitions", file_id=file_id)
        return []
    
    # Build filter expression like async_search
    expr_parts = []
    if partition != ["all"]:
        expr_parts.append(f"partition in {partition}")
    expr_parts.append(f'file_id == "{file_id}"')
    filter_expr = " and ".join(expr_parts) if expr_parts else ""
    
    # Query with filter
    results = await self._client.query_iterator(...)
    # ... return Document list


async def get_chunks_by_file_ids(
    self, 
    file_ids: list[str], 
    partition: list[str] | None,
    include_id: bool = True
) -> list[Document]:
    """Retrieve chunks for given file_ids in parallel, grouped and ordered by file_id."""
    # ... parallel retrieval with asyncio.gather()
```

**Key Changes:**
- Uses `asyncio.gather()` for parallel retrieval
- Helper method `_retrieve_file_chunks()` for single file retrieval
- **File existence check** before querying (prevents empty queries)
- Filter expression like `async_search` (handles `["all"]` and partition lists)
- No chunk limits in v1 (added in v2)

### 3. Pipeline Integration (`openrag/components/pipeline.py`)

```python
async def _prepare_for_chat_completion(self, partition: list[str] | None, payload: dict):
    messages = payload["messages"]
    messages = messages[-self.chat_history_depth :]
    
    metadata = payload.get("metadata") or {}
    attachments_raw = metadata.get("attachments")
    
    # Validate and extract file_ids from attachments
    file_ids: list[str] = []
    if attachments_raw:
        attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
        file_ids = [att.id for att in attachments if att.id]
    
    use_map_reduce = metadata.get("use_map_reduce", False)
    spoken_style_answer = metadata.get("spoken_style_answer", False)
    use_websearch = metadata.get("websearch", False)
    workspace = metadata.get("workspace")
    
    # FILE_ID RETRIEVAL MODE (skip semantic search)
    if file_ids:
        log = self.logger.bind(file_ids=file_ids, mode="file_based_retrieval")
        log.info("File-based retrieval mode enabled")
        
        # Retrieve chunks directly by file_id (parallel retrieval)
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        try:
            docs = await call_ray_actor_with_timeout(
                vectordb.get_chunks_by_file_ids.remote(
                    file_ids=file_ids,
                    partition=partition
                ),
                timeout=VECTORDB_TIMEOUT,
                task_description=f"get_chunks_by_file_ids({len(file_ids)} files)"
            )
            log.debug(f"Retrieved {len(docs)} chunks from {len(file_ids)} files")
        except TimeoutError as e:
            # Timeout handling - log and return empty docs
            log.error(f"Timeout retrieving chunks for file_ids", 
                     timeout=VECTORDB_TIMEOUT, error=str(e))
            docs = []
        
        # Create dummy queries for logging consistency
        queries = SearchQueries(query_list=[messages[-1]["content"]])
        web_results = []
    
    # NORMAL SEMANTIC SEARCH MODE
    elif partition is not None and use_websearch:
        # ... existing web search + RAG logic ...
    
    elif partition is not None:
        # ... existing RAG logic ...
    
    else:
        # ... existing web-only/direct LLM logic ...
    
    # Continue with context formatting and LLM call (unchanged)
    # ...
```

## Testing Strategy

### Unit Tests

1. **Model validation** (`openrag/models/test_openai.py` or inline)
   - Verify `Attachment` model accepts valid dict input
   - Verify `Attachment.id` is required and non-empty
   - Verify extra fields are ignored
   - Verify `attachments` defaults to `None` in metadata

2. **Vectordb method** (new file: `openrag/components/indexer/vectordb/test_file_id_retrieval.py`)
   - Test with valid file_ids in correct partition
   - Test with non-existent file_ids (returns empty, logs warning)
   - Test with mixed valid/invalid file_ids
   - Test with empty file_ids list (returns empty)
   - Verify chunk ordering matches file_id order
   - Test partition mismatch (file in wrong partition)
   - Test MilvusException handling (raises VDBError)
   - Test parallel execution (verify all files retrieved concurrently)

3. **Pipeline integration** (new file: `openrag/components/test_file_attachment_pipeline.py`)
   - Test file_id retrieval bypasses semantic search
   - Test empty attachments falls back to semantic search
   - Test invalid attachment format is skipped gracefully
   - Test timeout handling (returns empty docs, logs error)
   - Test Attachment model validation

### Integration Tests

1. **API test** (`tests/api_tests/test_openai_compat.py`)
   - POST `/v1/chat/completions` with `metadata.attachments`
   - Verify response contains chunks from specified files
   - Verify no semantic search occurs (check logs)
   - Test with non-existent file_ids (empty context, LLM responds)
   - Test chunk limit behavior with large files
   - Test cross-partition access when `partition=None` (verify intentional behavior)

### Security Tests

1. **Injection attack test**
   - Test with SQL injection in file_id (e.g., `"'; DROP TABLE...`)
   - Verify Milvus parameterized queries prevent injection

## Edge Cases

1. **Empty attachments list** → Falls back to semantic search
2. **All file_ids invalid** → Returns empty context, LLM responds without RAG
3. **Partition mismatch** → File_ids not in specified partition return no chunks (warning logged)
4. **Malformed attachment** → Silently skipped (missing/empty "id" field)
5. **Ray actor timeout** → Returns empty docs, error logged, LLM responds without RAG
6. **Multiple partitions provided** → Uses first partition only (warning logged)
7. **Milvus connection error** → Raises VDBError with specific error code
8. **Large files** → All chunks retrieved (no limits in v1, context limits apply later)

## Future Enhancements

1. **Hybrid mode**: Combine file_id retrieval with semantic search
2. **Chunk limits**: Add `max_chunks_per_file` and `max_total_chunks` (v2)
3. **Additional attachment metadata**: Support file type hints, custom metadata, priority ranking
4. **Re-ranking**: Apply reranking to file-based chunks
5. **Response metadata**: Return attachment processing status in response

## Known Limitations (v1.0)

**Authorization:** File access authorization is not enforced in this version. All users can access any file_id. Future versions will add user context validation.

**Mitigation:** Use partition-based isolation for multi-tenant scenarios. Only expose file_ids to users who should have access.

**No Chunk Limits:** All chunks are retrieved per file without limits. Context token limits will be applied during formatting. Large files with many chunks may exceed LLM context window.

**Mitigation:** Monitor chunk counts and add limits in v2 if needed.

## Dependencies

- No new dependencies required
- Uses existing Ray actor pattern
- Uses existing vectordb infrastructure

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Breaking existing metadata format | New field with `None` default, backward compatible |
| Performance with large files | No limits in v1, context formatting handles token overflow |
| Confusion with workspace filter | They are mutually exclusive in practice (workspace implies multiple files) |
| Silent failures confusing users | Comprehensive logging at warning/error levels |
| Partition ambiguity | Single partition enforced, warnings for multiple partitions |
| Timeout errors | Graceful degradation (empty docs, error logged) |
| Milvus errors | Specific exception handling with VDBError codes |
| Future auth requirements | Current design allows adding user param later |
| Large chunk counts | Monitor usage, add limits in v2 if needed |

## Success Criteria

- [ ] Users can provide file IDs via `metadata.attachments`
- [ ] System retrieves chunks only from specified files (semantic search bypassed)
- [ ] Chunk ordering matches file_id order
- [ ] Empty/invalid file_ids handled gracefully (logs warning, continues)
- [ ] Timeout errors handled gracefully (empty docs, error logged)
- [ ] Milvus errors raise specific VDBError with code
- [ ] Parallel retrieval implemented (asyncio.gather)
- [ ] Attachment model validation works correctly
- [ ] No breaking changes to existing API
- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] SQL injection attempts blocked (parameterized queries)
