# File Reducer Design

**Date:** 2026-03-26  
**Author:** OpenRAG Team  
**Status:** Approved  
**Review Status:** Approved by spec review

## Overview

Add on-demand chunk summarization for file attachments that exceed the context token limit. This feature provides two summarization strategies: **Refine** (iterative) and **Map-Reduce** (parallel).

## Problem Statement

When retrieving chunks from attached files, the total token count may exceed the model's context window. Currently, the system truncates context without intelligent summarization, potentially losing important information.

## Solution

Implement a `FileReducer` class that:
1. Detects when retrieved chunks exceed the token limit
2. Applies summarization using the user-selected strategy
3. Returns condensed chunks within the target token limit

## Architecture

### Components

#### 1. FileReducer Class

**Location:** `openrag/components/file_reducer.py`

```python
class FileReducer:
    """Reduces document chunks to fit within token limits using summarization."""
    
    def __init__(self, config, llm_client):
        """Initialize FileReducer.
        
        Args:
            config: Configuration object with file_reducer settings
            llm_client: ChatOpenAI instance for summarization
        """
        self.config = config
        self.llm = llm_client
        self.max_tokens = config.file_reducer.get("max_tokens", 512)
        self.token_counter = llm_client.get_num_tokens
        self.timeout = config.file_reducer.get("timeout", 120)
        self.temperature = config.file_reducer.get("temperature", 0.3)
        self.max_chunks_refine = config.file_reducer.get("max_chunks_refine", 10)
```

**Public Methods:**

```python
async def reduce(self, chunks: list[Document], strategy: str) -> list[Document]:
    """Reduce chunks if they exceed the token limit.
    
    Args:
        chunks: List of document chunks to potentially reduce
        strategy: Either "refine" or "map_reduce"
        
    Returns:
        Reduced list of chunks (or original if under limit)
        
    Raises:
        ValueError: If strategy is not recognized
    """
    # Edge cases
    if not chunks:
        return []
    
    if len(chunks) == 1:
        return chunks  # No reduction needed
    
    # Calculate tokens
    total_content = "\n".join(chunk.page_content for chunk in chunks)
    total_tokens = self.token_counter(total_content)
    
    if total_tokens <= self.max_tokens:
        return chunks  # Under limit
    
    # Auto-switch strategy if too many chunks for refine
    if strategy == "refine" and len(chunks) > self.max_chunks_refine:
        logger.warning(
            "Switching from refine to map_reduce due to chunk count",
            chunk_count=len(chunks),
            max_chunks=self.max_chunks_refine,
        )
        strategy = "map_reduce"
    
    # Apply strategy
    if strategy == "refine":
        return await self._refine_summarization(chunks, total_tokens)
    else:
        return await self._map_reduce_summarization(chunks, total_tokens)
```

**Private Methods:**

```python
async def _refine_summarization(self, chunks: list[Document], total_tokens: int) -> list[Document]:
    """Iterative refinement summarization.
    
    Process chunks sequentially where each summary becomes context for the next:
    1. Summarize first chunk -> initial_summary
    2. For each subsequent chunk: summarize(initial_summary + chunk) -> new_summary
    3. Return final summary as single chunk
    
    Args:
        chunks: List of document chunks
        total_tokens: Pre-calculated token count
        
    Returns:
        Single chunk containing refined summary
    """

async def _map_reduce_summarization(self, chunks: list[Document], total_tokens: int) -> list[Document]:
    """Map-Reduce summarization.
    
    Process chunks in parallel then combine:
    1. Map: Summarize each chunk independently
    2. Reduce: Combine all summaries and summarize again
    3. Return consolidated summary as single chunk
    
    Args:
        chunks: List of document chunks
        total_tokens: Pre-calculated token count
        
    Returns:
        Single chunk containing consolidated summary
    """
```

#### 2. RagPipeline Integration

**Location:** `openrag/components/pipeline.py`

**Changes to `__init__()`:**
```python
class RagPipeline:
    def __init__(self):
        # ... existing initialization ...
        from .file_reducer import FileReducer
        self.file_reducer = FileReducer(config, self.llm_client)
```

**Changes to `_prepare_for_chat_completion()`:**
```python
# After file-based retrieval (around line 218-234)
if file_ids:
    # ... existing retrieval code ...
    
    # Apply file reduction if strategy specified on any attachment
    # Priority: file_reduction_strategy > use_map_reduce (mutually exclusive for file attachments)
    # Extract strategy from first attachment (default: "refine")
    attachments = metadata.get("attachments", [])
    strategy = attachments[0].get("strategy", "refine") if attachments else None
    
    if strategy:
        docs = await self.file_reducer.reduce(docs, strategy=strategy)
    elif use_map_reduce and docs:
        docs = await self.map_reduce.map(query=queries.query_list[0], chunks=docs)
```

**Note:** Strategy is extracted from the attachment itself, defaulting to `"refine"` if not specified.

### Data Flow

```
API Request
    |
OpenAIChatCompletionRequest (metadata.file_reduction_strategy)
    |
RagPipeline._prepare_for_chat_completion()
    |
Extract file_ids from attachments
    |
Retrieve chunks via Vectordb.get_chunks_by_file_ids()
    |
Check: file_reduction_strategy in metadata?
    | YES
FileReducer.reduce(chunks, strategy)
    |
Calculate: token_counter(concatenated_chunks)
    |
Check: total_tokens > max_tokens?
    | YES
Apply strategy (_refine or _map_reduce)
    |
Return reduced chunk(s)
    |
Continue normal RAG pipeline
```

## Configuration

**File:** `.hydra_config/config.yaml` (add to existing config, not separate file)

```yaml
file_reducer:
  # Target maximum tokens for reduced output
  max_tokens: ${oc.decode:${oc.env:FILE_REDUCER_MAX_TOKENS, 512}}
  
  # Timeout for summarization LLM calls (seconds)
  timeout: ${oc.decode:${oc.env:FILE_REDUCER_TIMEOUT, 120}}
  
  # Temperature for summarization generation
  temperature: ${oc.decode:${oc.env:FILE_REDUCER_TEMPERATURE, 0.3}}
  
  # Maximum chunks for refine strategy before switching to map_reduce
  max_chunks_refine: ${oc.decode:${oc.env:FILE_REDUCER_MAX_CHUNKS_REFINE, 10}}
```

## API Changes

### Request Model

**File:** `openrag/models/openai.py`

**Remove MetadataDict TypedDict** - validation is handled by Attachment class:

**Update Attachment model to include strategy:**
```python
class Attachment(BaseModel):
    """Represents a file attachment for RAG retrieval."""
    
    id: str = Field(..., min_length=1, description="File ID")
    type: Literal["file"] | None = Field(None, description="For future extensibility")
    priority: int | None = Field(None, ge=0, description="For future ranking")
    strategy: Literal["refine", "map_reduce"] | None = Field(
        "refine",  # Default strategy
        description="Chunk reduction strategy when file exceeds token limit."
    )
```

**Update metadata field to use dict[str, Any]:**
```python
class OpenAIChatCompletionRequest(BaseModel):
    # ... existing fields ...
    metadata: dict[str, Any] | None = Field(
        default_factory=dict,
        description=(
            "Extra custom parameters. "
            "Supports 'attachments' for file-based retrieval (each attachment has 'id' and optional 'strategy' field: 'refine' or 'map_reduce', defaults to 'refine'), "
            "'use_map_reduce' for semantic search summarization."
        ),
    )
```

### Usage Example

```json
{
  "model": "openrag-model",
  "messages": [
    {
      "role": "user",
      "content": "Summarize the attached document"
    }
  ],
  "metadata": {
    "attachments": [
      {"id": "file-123", "strategy": "refine"},
      {"id": "file-456", "strategy": "map_reduce"},
      {"id": "file-789"}  // Uses default strategy: "refine"
    ]
  }
}
```

**Default Strategy:** If `strategy` is not specified on an attachment, it defaults to `"refine"`.

## Implementation Details

### Imports

```python
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from utils.logger import get_logger
from .map_reduce import system_prompt_map  # Reuse existing prompt
from .utils import get_llm_semaphore

logger = get_logger()
```

### System Prompts

**Refine Strategy:**
```python
SYSTEM_PROMPT_REFINE = """You are an AI assistant specialized in iterative document summarization.

Your task:
1. Combine the previous summary with new content into a cohesive, updated summary
2. Preserve key information: names, dates, technical terms, project identifiers
3. Maintain the original language of the content
4. Stay within the token limit while maximizing information density

Guidelines:
- Do not add commentary or rephrasing beyond what's necessary
- Keep the summary self-contained (it should be understandable without context)
- Prioritize information that directly addresses potential user queries"""
```

**Map-Reduce Strategy:** Use the **existing** system prompt from `openrag/components/map_reduce.py`:
```python
# Import from existing module
from .map_reduce import system_prompt_map  # Reuse existing prompt
```

This ensures consistency with the existing `use_map_reduce` feature.

### Token Calculation

```python
# In FileReducer.reduce()
# Note: Token calculation is for decision-making only
# Actual prompts include additional overhead (system prompts, instructions)
total_content = "\n".join(chunk.page_content for chunk in chunks)
total_tokens = self.token_counter(total_content)

if total_tokens <= self.max_tokens:
    return chunks  # No reduction needed
```

**Note:** The `max_tokens` limit applies to the output summary, not the input. The LLM is instructed to stay within the limit during summarization.

### Helper: Metadata Merge

```python
def _merge_metadata(self, original_chunks: list[Document]) -> dict:
    """Merge metadata from multiple chunks, preserving key fields."""
    base = original_chunks[0].metadata.copy()
    # Mark as summarized
    base["_summarized"] = True
    base["_original_chunk_count"] = len(original_chunks)
    # Preserve file_id and partition from first chunk
    base["file_id"] = original_chunks[0].metadata.get("file_id")
    base["partition"] = original_chunks[0].metadata.get("partition")
    return base
```

### Refine Strategy Implementation

```python
async def _refine_summarization(self, chunks: list[Document], total_tokens: int) -> list[Document]:
    """Iterative refinement summarization."""
    summary = chunks[0].page_content
    
    for i, chunk in enumerate(chunks[1:], start=2):
        prompt = f"""Previous summary:
{summary}

New content to integrate:
{chunk.page_content}

Create an updated summary that combines both, staying within {self.max_tokens} tokens:"""
        
        async with get_llm_semaphore():
            response = await self.llm.ainvoke([
                {"role": "system", "content": SYSTEM_PROMPT_REFINE},
                {"role": "user", "content": prompt}
            ])
            summary = response.content
    
    return [Document(page_content=summary, metadata=self._merge_metadata(chunks))]
```

### Map-Reduce Strategy Implementation

```python
async def _map_reduce_summarization(self, chunks: list[Document], total_tokens: int) -> list[Document]:
    """Map-Reduce summarization using existing system prompt."""
    # Map phase: summarize each chunk independently
    async def summarize_chunk(chunk: Document) -> str:
        prompt = f"""Summarize this content concisely, keeping key information:
{chunk.page_content}"""
        
        async with get_llm_semaphore():
            response = await self.llm.ainvoke([
                {"role": "system", "content": system_prompt_map},  # Use existing prompt
                {"role": "user", "content": prompt}
            ])
            return response.content
    
    summaries = await asyncio.gather(*[summarize_chunk(c) for c in chunks])
    combined = "\n\n".join(summaries)
    
    # Check if combined summaries fit within limit
    combined_tokens = self.token_counter(combined)
    if combined_tokens <= self.max_tokens:
        final_summary = combined
    else:
        # Need recursive reduction
        reduce_prompt = f"""Combine these summaries into one cohesive summary:
{combined}

Stay within {self.max_tokens} tokens:"""
        
        async with get_llm_semaphore():
            response = await self.llm.ainvoke([{"role": "user", "content": reduce_prompt}])
            final_summary = response.content
    
    return [Document(page_content=final_summary, metadata=self._merge_metadata(chunks))]
```

## Error Handling

1. **LLM Timeout:** Log warning, return original chunks unchanged
2. **Empty Input:** Return empty list
3. **Single Chunk:** Return as-is (no reduction needed)
4. **Invalid Strategy:** Raise `ValueError` with clear message
5. **LLM Error:** Log error, return original chunks unchanged

```python
try:
    # summarization logic
except Exception as e:
    logger.warning(
        "File reduction failed, using original chunks",
        error=str(e),
        strategy=strategy,
    )
    return chunks
```

## Testing

### Unit Tests

**File:** `openrag/components/test_file_reducer.py`

```python
@pytest.mark.unit
class TestFileReducer:
    def test_reduce_under_limit(self):
        """Should return original chunks if under token limit."""
    
    def test_reduce_refine_strategy(self):
        """Should apply refine summarization."""
    
    def test_reduce_map_reduce_strategy(self):
        """Should apply map-reduce summarization."""
    
    def test_reduce_invalid_strategy(self):
        """Should raise ValueError for unknown strategy."""
    
    def test_reduce_empty_chunks(self):
        """Should return empty list for empty input."""
    
    def test_reduce_single_chunk(self):
        """Should return single chunk unchanged."""
    
    def test_metadata_preservation(self):
        """Should preserve file_id and partition in metadata."""
        chunks = [
            Document(page_content="test", metadata={"file_id": "file-123", "partition": "docs"})
        ]
        result = await reducer.reduce(chunks, "refine")
        assert result[0].metadata["file_id"] == "file-123"
        assert result[0].metadata["partition"] == "docs"
        assert result[0].metadata["_summarized"] is True
    
    async def test_timeout_fallback(self, monkeypatch):
        """Should return original chunks on LLM timeout."""
        # Mock LLM to timeout
        monkeypatch.setattr(self.llm, "ainvoke", asyncio.sleep(1000))
        result = await reducer.reduce(chunks, "refine")
        assert result == chunks  # Original chunks returned
    
    def test_output_within_tokens(self):
        """Should produce output within max_tokens limit."""
        # Large input chunks
        result = await reducer.reduce(large_chunks, "refine")
        output_tokens = self.token_counter(result[0].page_content)
        assert output_tokens <= self.max_tokens
    
    def test_auto_switch_to_map_reduce(self):
        """Should switch to map_reduce when chunks exceed max_chunks_refine."""
        many_chunks = [Document(page_content=f"chunk {i}") for i in range(15)]
        result = await reducer.reduce(many_chunks, "refine")
        # Should have switched to map_reduce automatically
        assert len(result) == 1
```

### Integration Tests

**File:** `tests/api_tests/test_file_reduction.py`

```python
@pytest.mark.integration
class TestFileReductionAPI:
    async def test_file_reduction_refine(self):
        """Test API with refine strategy."""
    
    async def test_file_reduction_map_reduce(self):
        """Test API with map-reduce strategy."""
    
    async def test_file_reduction_no_strategy(self):
        """Test API without reduction (normal retrieval)."""
```

## Performance Considerations

1. **Token Calculation:** O(n) where n = total characters in all chunks
2. **Refine Strategy:** O(k) LLM calls where k = number of chunks (limited to `max_chunks_refine`)
3. **Map-Reduce Strategy:** O(k + 1) LLM calls (k maps + 1 reduce)
4. **Concurrency:** Use `asyncio.gather()` for map phase parallelization
5. **Timeout:** LLM client initialized with timeout to prevent hangs
6. **Auto-switch:** Refine automatically switches to Map-Reduce if chunks > `max_chunks_refine` (default: 10)

## Trade-offs

### Refine vs Map-Reduce

| Aspect | Refine | Map-Reduce |
|--------|--------|------------|
| Context Preservation | High (accumulates context) | Medium (independent summaries) |
| Speed | Slower (sequential) | Faster (parallel map phase) |
| Token Efficiency | Better for long documents | Better for diverse content |
| LLM Calls | k calls | k+1 calls |

### When to Use Each

- **Refine:** Documents with strong sequential dependency (chapters, reports)
- **Map-Reduce:** Documents with independent sections (research papers, multi-topic docs)

## Future Enhancements

1. **Hybrid Strategy:** Combine both approaches adaptively
2. **Chunk-level Reduction:** Reduce to multiple chunks instead of single summary
3. **Caching:** Cache summaries for repeated documents
4. **Streaming:** Support streaming summaries for long documents

## Dependencies

- No new external dependencies
- Uses existing LLM client (ChatOpenAI)
- Leverages existing `get_llm_semaphore()` for rate limiting

## Migration Notes

- **Breaking Change:** `MetadataDict` TypedDict removed
- **Migration:** Use `dict[str, Any]` for metadata field instead
- **Attachment Model Extended:** Added `strategy` field with default `"refine"`
- **Backward Compatible:** Existing API calls without `strategy` work unchanged (defaults to "refine")
- **Config Addition:** New `file_reducer` section added to `.hydra_config/config.yaml`
- **Reuses Existing Prompt:** Map-Reduce strategy uses existing `system_prompt_map` from `map_reduce.py`
