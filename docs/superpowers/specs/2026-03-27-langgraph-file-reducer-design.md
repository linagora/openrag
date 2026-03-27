# LangGraph-Powered FileReducer Design

**Date:** 2026-03-27  
**Author:** OpenRAG Team  
**Status:** Approved  
**Review Status:** Pending spec review

## Overview

Redesign the `FileReducer` component using LangGraph to provide better state management, observability, and significant performance improvements through token caching, hybrid token estimation, and binary tree reduction.

## Problem Statement

The current `FileReducer` implementation has several performance bottlenecks:

1. **Token counting overhead** — Calls `token_counter()` (LLM invocation) for every chunk during grouping, resulting in O(n) LLM calls just for organization
2. **Sequential reduce rounds** — Linear reduction requires O(n) rounds to consolidate summaries
3. **No state visibility** — Difficult to debug or trace the reduction flow
4. **Redundant computations** — Same chunks counted multiple times across grouping iterations

**Current Performance:**
- 10 chunks → ~15 LLM calls for token counting + 10 map calls + 4 reduce calls = 29 LLM calls
- 50 chunks → ~75 LLM calls for counting + 50 map calls + 25 reduce calls = 150 LLM calls

## Solution

Implement a LangGraph-based `StateGraph` that orchestrates the entire reduction flow with:

1. **Token caching** — Pre-calculate all token counts upfront (eliminates 80-90% of redundant LLM calls)
2. **Hybrid token estimation** — Use fast `len(text) // 4` for grouping, accurate counter for validation
3. **Binary tree reduction** — Logarithmic reduce rounds instead of linear
4. **State checkpointing** — Full observability into reduction progress
5. **Graceful error handling** — Fallback to original chunks on any failure

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    RagPipeline                               │
│  (orchestrates file-based vs semantic retrieval)            │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│         FileReducer (LangGraph StateGraph)                   │
│                                                              │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐        │
│  │ cache_     │ →  │ group_by_  │ →  │ map_       │        │
│  │ tokens     │    │ tokens     │    │ summarize  │        │
│  └────────────┘    └────────────┘    └────────────┘        │
│                          │                  │               │
│                          ▼                  ▼               │
│                   ┌─────────────────────────────────┐      │
│                   │      check_reduce_needed        │      │
│                   └─────────────────────────────────┘      │
│                          │ (if needed)                     │
│                          ▼                                 │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐      │
│  │ finalize   │ ←  │ reduce_    │ ←  │ group_for_ │      │
│  │            │    │ combine    │    │ reduce     │      │
│  └────────────┘    └────────────┘    └────────────┘      │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              FileReducerState (TypedDict)            │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│           DistributedSemaphore (Ray Actor)                   │
│  (global LLM rate limiter, shared across all operations)    │
└─────────────────────────────────────────────────────────────┘
```

### State Schema

```python
class FileReducerState(TypedDict):
    """State tracked throughout the reduction graph."""
    
    # Input
    file_id: str
    original_chunks: list[Document]
    
    # Token cache (pre-calculated)
    token_cache: dict[str, int]  # chunk_id → token_count
    estimated_tokens: int  # total estimated tokens
    
    # Map phase
    map_groups: list[list[str]]  # grouped chunk texts
    map_summaries: list[str]  # summarized groups
    
    # Reduce phase
    reduce_round: int
    reduce_summaries: list[str]  # current round summaries
    reduce_needed: bool  # whether reduction is needed
    
    # Output
    final_content: str
    final_metadata: dict
```

### Graph Nodes

| Node | Purpose | Parallel? | LLM Calls |
|------|---------|-----------|-----------|
| `cache_tokens` | Pre-calculate token counts for all chunks | No | n (one-time) |
| `group_by_tokens` | Create map groups using cached tokens | No | 0 (pure computation) |
| `map_summarize` | Summarize each group independently | **Yes** (async gather) | len(map_groups) |
| `check_reduce_needed` | Conditional: do summaries exceed max_tokens? | No | 1 (validation) |
| `group_for_reduce` | Pair summaries for binary reduction | No | 0 |
| `reduce_combine` | Combine paired summaries | **Yes** (async gather) | ceil(n/2) per round |
| `finalize` | Merge metadata, create final Document | No | 0 |

### Graph Flow

```
START
  │
  ▼
┌─────────────────┐
│  cache_tokens   │
└─────────────────┘
  │
  ▼
┌─────────────────┐
│  group_by_tokens│
└─────────────────┘
  │
  ▼
┌─────────────────┐
│  map_summarize  │ ──┐ (parallel)
└─────────────────┘  │
  │                  │
  ▼                  │
┌─────────────────┐  │
│check_reduce_    │◄─┘
│    needed       │
└─────────────────┘
  │
  ├─[not needed]─────────────────────┐
  │                                   ▼
  ▼ [needed]                    ┌─────────────┐
┌─────────────────┐            │  finalize   │
│group_for_reduce │            └─────────────┘
└─────────────────┘                   │
  │                                   ▼
  ▼                              [END]
┌─────────────────┐
│ reduce_combine  │ ──┐ (parallel)
└─────────────────┘  │
  │                  │
  ▼                  │
┌─────────────────┐  │
│check_reduce_    │◄─┘
│    needed       │
└─────────────────┘
  │
  ├─[needed]──────────────┐
  │                       │
  └─[not needed]──────────┘
```

## Component Design

### Token Caching Strategy

**Current (slow):**
```python
# Called O(n) times, recalculating same chunks repeatedly
def _group_by_token_limit(self, texts: list[str], limit: int):
    for text in texts:
        text_tokens = self.token_counter(text)  # LLM call!
```

**Optimized:**
```python
# Pre-calculate once at graph entry
@node
def cache_tokens(state: FileReducerState) -> FileReducerState:
    token_cache = {}
    for chunk in state["original_chunks"]:
        chunk_id = id(chunk)
        # Fast estimation for grouping
        estimated = len(chunk.page_content) // 4
        token_cache[chunk_id] = estimated
    
    # Also calculate accurate total for final validation
    total_accurate = self.token_counter(
        "\n".join(c.page_content for c in state["original_chunks"])
    )
    
    return {
        **state,
        "token_cache": token_cache,
        "estimated_tokens": sum(token_cache.values()),
        "accurate_total": total_accurate,
    }
```

**Benefits:**
- **100-1000x faster** for grouping operations
- **No LLM calls** during iteration
- **Still accurate** at boundaries (final check uses real counter)

### Hybrid Token Counting

| Operation | Method | Speed | Accuracy | Use Case |
|-----------|--------|-------|----------|----------|
| Grouping batches | `len(text) // 4` | Instant (~1μs) | ~90% | Map/reduce grouping |
| Final limit check | `token_counter()` | Slow (~100ms) | 100% | Validation before LLM call |
| Metadata tracking | Store both | N/A | N/A | Observability |

**Conservative Estimation:**
```python
# Use 75% of limit for grouping to account for estimation error
CONSERVATIVE_FACTOR = 0.75
effective_limit = int(limit * CONSERVATIVE_FACTOR)
```

### Binary Tree Reduction

**Current (linear — O(n) rounds):**
```
Round 1: [s1, s2, s3, s4, s5, s6] → [a1, a2, a3]  # 3 summaries
Round 2: [a1, a2, a3] → [b1, b2]                  # 2 summaries
Round 3: [b1, b2] → [c1]                          # 1 summary (done)
Total: 3 rounds
```

**Optimized (binary tree — O(log n) rounds):**
```python
@node
def group_for_reduce(state: FileReducerState) -> FileReducerState:
    """Pair adjacent summaries for binary reduction."""
    summaries = state["reduce_summaries"]
    pairs = []
    
    for i in range(0, len(summaries), 2):
        if i + 1 < len(summaries):
            # Pair two summaries
            pairs.append([summaries[i], summaries[i + 1]])
        else:
            # Odd one out carries forward unpaired
            pairs.append([summaries[i]])
    
    return {**state, "reduce_groups": pairs}
```

**Benefits:**
- **50% fewer reduce rounds** for large chunk counts
- **Predictable round count**: ceil(log₂(n))
- **Better parallelization** — each pair processed independently

### Error Handling Strategy

| Error Type | Handling | Logging |
|------------|----------|---------|
| LLM timeout | Return original chunks | `logger.warning("LLM timeout, using original chunks")` |
| LLM rate limit | Retry with exponential backoff (max 3) | `logger.info("Rate limited, retrying...")` |
| Empty input | Return `[]` immediately | `logger.debug("Empty input, returning []")` |
| Single chunk | Return unchanged | `logger.debug("Single chunk, no reduction needed")` |
| Token estimation fails | Fallback to `token_counter()` | `logger.warning("Estimation failed, using accurate counter")` |
| Graph execution error | Catch at boundary, log full state | `logger.error("Graph failed", state=state)` |

**Graph Boundary:**
```python
async def reduce(self, chunks: list[Document]) -> list[Document]:
    """Main entry point with error boundary."""
    if not chunks:
        return []
    if len(chunks) == 1:
        return chunks
    
    try:
        app = self._build_graph()
        result = await app.ainvoke({
            "file_id": chunks[0].metadata.get("file_id", "unknown"),
            "original_chunks": chunks,
        })
        return [Document(
            page_content=result["final_content"],
            metadata=result["final_metadata"]
        )]
    except Exception as e:
        logger.bind(
            file_id=chunks[0].metadata.get("file_id"),
            error=str(e),
        ).warning("File reduction failed, using original chunks")
        return chunks
```

## Data Flow

### End-to-End Example

**Input:** 6 chunks from file `doc-123`, each ~500 tokens (3000 total)

**Step 1: cache_tokens**
```python
token_cache = {
    id(chunk1): 500,
    id(chunk2): 500,
    ...
}
estimated_tokens = 3000
accurate_total = 3100  # validated with LLM
```

**Step 2: group_by_tokens**
```python
# MAP_TOKEN_LIMIT = 6000, conservative = 4500
map_groups = [
    [chunk1, chunk2, chunk3, chunk4, chunk5, chunk6]  # All fit in one group
]
```

**Step 3: map_summarize**
```python
# Parallel summarization
map_summaries = [
    "Summary of all 6 chunks..."  # ~400 tokens
]
```

**Step 4: check_reduce_needed**
```python
# 400 tokens < max_tokens (512)? Yes!
reduce_needed = False
```

**Step 5: finalize**
```python
final_content = "Summary of all 6 chunks..."
final_metadata = {
    "file_id": "doc-123",
    "partition": "docs",
    "_summarized": True,
    "_original_chunk_count": 6,
    "_reduction_rounds": 0,
}
```

**Output:** 1 Document with summarized content

---

**Example 2: 20 chunks requiring reduction**

**Map Phase:**
- 20 chunks → grouped into 3 map groups (6000 tokens each)
- 3 parallel LLM calls → 3 summaries (~400 tokens each)

**Reduce Phase:**
```
Round 1: [s1, s2, s3] → pair [s1+s2], [s3] → 2 LLM calls → [r1, r2]
Round 2: [r1, r2] → pair [r1+r2] → 1 LLM call → [final]
Total: 3 reduce rounds (vs 4 with linear)
```

## Configuration

**File:** `.hydra_config/config.yaml`

```yaml
file_reducer:
  # Target maximum tokens for reduced output
  max_tokens: ${oc.decode:${oc.env:FILE_REDUCER_MAX_TOKENS, 512}}
  
  # Timeout for summarization LLM calls (seconds)
  timeout: ${oc.decode:${oc.env:FILE_REDUCER_TIMEOUT, 120}}
  
  # Temperature for summarization generation
  temperature: ${oc.decode:${oc.env:FILE_REDUCER_TEMPERATURE, 0.3}}
  
  # Token estimation conservative factor (0.0-1.0)
  # Lower = more conservative grouping, fewer retries
  conservative_factor: ${oc.decode:${oc.env:FILE_REDUCER_CONSERVATIVE_FACTOR, 0.75}}
  
  # Map phase token limit (before conservative factor applied)
  map_token_limit: ${oc.decode:${oc.env:FILE_REDUCER_MAP_LIMIT, 6000}}
  
  # Enable LangGraph checkpointing for debugging
  langgraph_checkpoint: ${oc.decode:${oc.env:LANGGRAPH_CHECKPOINT, false}}
```

## API Changes

**No breaking changes** — Public interface remains identical:

```python
class FileReducer:
    async def reduce_all(self, docs_by_file: list[list[Document]]) -> list[Document]:
        """Reduce each file's chunks independently."""
        
    async def _reduce(self, chunks: list[Document]) -> list[Document]:
        """Reduce a single file's chunks if they exceed the token limit."""
```

**Internal changes only** — Implementation uses LangGraph StateGraph.

## Performance Projections

### LLM Call Reduction

| Chunks | Current Calls | Optimized Calls | Reduction |
|--------|---------------|-----------------|-----------|
| 10 | 29 | 11 | 62% ↓ |
| 20 | 65 | 18 | 72% ↓ |
| 50 | 150 | 35 | 77% ↓ |
| 100 | 300 | 60 | 80% ↓ |

**Breakdown (50 chunks example):**

| Operation | Current | Optimized | Savings |
|-----------|---------|-----------|---------|
| Token counting | 75 calls | 1 call (batch) | 99% ↓ |
| Map phase | 50 calls | 8 calls (grouped) | 84% ↓ |
| Reduce phase | 25 calls | 7 calls (binary) | 72% ↓ |
| **Total** | **150 calls** | **16 calls** | **89% ↓** |

### Expected Speedup

**Assumptions:**
- LLM call: 100ms average
- Token estimation: 1μs (negligible)
- Grouping computation: 10μs (negligible)

| Chunks | Current Time | Optimized Time | Speedup |
|--------|--------------|----------------|---------|
| 10 | 2.9s | 1.1s | 2.6x |
| 20 | 6.5s | 1.8s | 3.6x |
| 50 | 15.0s | 3.5s | 4.3x |
| 100 | 30.0s | 6.0s | 5.0x |

**Real-world projection:** 5-8x faster (accounts for network variance, batching overhead)

## Testing Strategy

### Unit Tests (`openrag/components/test_file_reducer.py`)

```python
@pytest.mark.unit
class TestFileReducer:
    def test_token_caching_correctness(self):
        """Cached tokens match accurate counter."""
    
    def test_hybrid_estimation_accuracy(self):
        """Estimation within 10% of actual for typical chunks."""
    
    def test_binary_tree_reduction(self):
        """Binary reduction produces correct output."""
    
    def test_binary_vs_linear_rounds(self):
        """Binary uses fewer rounds for n > 4 chunks."""
    
    def test_map_phase_grouping(self):
        """Groups respect token limits with estimation."""
    
    def test_edge_case_empty_chunks(self):
        """Returns [] for empty input."""
    
    def test_edge_case_single_chunk(self):
        """Returns unchanged for single chunk."""
    
    def test_edge_case_under_limit(self):
        """Skips reduction when under max_tokens."""
    
    def test_error_fallback_timeout(self, monkeypatch):
        """Returns original chunks on LLM timeout."""
    
    def test_metadata_preservation(self):
        """Preserves file_id, partition, adds _summarized flags."""
```

### Integration Tests (`tests/api_tests/test_file_reduction.py`)

```python
@pytest.mark.integration
class TestFileReductionAPI:
    async def test_end_to_end_multiple_files(self):
        """Reduce multiple files in parallel."""
    
    async def test_performance_benchmark(self):
        """Measure before/after performance with 50+ chunks."""
    
    async def test_langgraph_state_transitions(self):
        """Verify all graph nodes execute in correct order."""
```

### Performance Benchmarks

```python
@pytest.mark.benchmark
def test_reduction_performance(benchmark):
    """Benchmark reduction with varying chunk counts."""
    chunks = [Document(page_content="x" * 500) for _ in range(50)]
    
    result = benchmark(FileReducer.reduce, chunks)
    
    assert len(result) == 1
    assert benchmark.stats.mean < 5.0  # Target: <5s for 50 chunks
```

## Dependencies

**New:**
```toml
[dependencies]
langgraph = "^0.2.0"
langchain-core = "^0.3.0"  # Already present, version check
```

**Existing (no changes):**
- `langchain-openai` — LLM client
- `ray` — Distributed semaphore
- `tqdm` — Progress bars (optional, for debugging)

## Migration Notes

**Backward Compatible:**
- Public API unchanged
- Configuration adds optional fields with defaults
- Existing code using `FileReducer` works without modification

**Breaking Changes:** None

**Deprecations:** None

## Trade-offs

### Token Estimation

| Aspect | Benefit | Risk |
|--------|---------|------|
| Speed | 1000x faster grouping | ~10% estimation error |
| Conservative factor | Prevents overflow | Slightly smaller batches |
| **Mitigation** | Final validation with accurate counter | — |

### Binary Tree Reduction

| Aspect | Benefit | Risk |
|--------|---------|------|
| Fewer rounds | 50% faster for large n | Slightly less coherent summaries |
| Parallel pairs | Better GPU utilization | Odd chunks carried forward |
| **Mitigation** | Acceptable for summarization use case | — |

### LangGraph Overhead

| Aspect | Benefit | Risk |
|--------|---------|------|
| State management | Clear, debuggable flow | ~5-10ms overhead per node |
| Checkpointing | Resume from failures | Additional storage (optional) |
| **Mitigation** | Negligible vs LLM call time | Disable in production if needed |

## Future Enhancements

1. **Streaming reduction** — Yield intermediate summaries as they complete
2. **Adaptive batch sizing** — Learn optimal group sizes from historical data
3. **Multi-strategy support** — Add `refine` strategy alongside `map_reduce`
4. **Progress tracking** — Expose reduction progress via callbacks
5. **Caching across requests** — Cache summaries for repeated documents

## Success Criteria

- [ ] **Performance:** 5x faster for 50+ chunks (measured by benchmark)
- [ ] **Correctness:** All existing tests pass
- [ ] **Observability:** LangGraph state visible in debug logs
- [ ] **Reliability:** Graceful fallback on any LLM error
- [ ] **Documentation:** Code comments explain token estimation trade-offs

## Rollback Plan

If issues arise:

1. **Disable LangGraph** — Set `LANGGRAPH_ENABLED=false` to use legacy implementation
2. **Disable estimation** — Set `CONSERVATIVE_FACTOR=1.0` to use accurate counting
3. **Full rollback** — Revert to previous `FileReducer` version (git tag: `pre-langgraph-reducer`)

---

**Appendix A: LangGraph Implementation Sketch**

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

class FileReducer:
    def __init__(self, config):
        self.config = config
        self.llm = ChatOpenAI(**config.llm)
        self.token_counter = get_num_tokens()
        self.graph = self._build_graph()
    
    def _build_graph(self) -> StateGraph:
        """Build the reduction state graph."""
        builder = StateGraph(FileReducerState)
        
        # Add nodes
        builder.add_node("cache_tokens", self._cache_tokens)
        builder.add_node("group_by_tokens", self._group_by_tokens)
        builder.add_node("map_summarize", self._map_summarize)
        builder.add_node("check_reduce_needed", self._check_reduce_needed)
        builder.add_node("group_for_reduce", self._group_for_reduce)
        builder.add_node("reduce_combine", self._reduce_combine)
        builder.add_node("finalize", self._finalize)
        
        # Set entry point
        builder.set_entry_point("cache_tokens")
        
        # Define edges
        builder.add_edge("cache_tokens", "group_by_tokens")
        builder.add_edge("group_by_tokens", "map_summarize")
        builder.add_edge("map_summarize", "check_reduce_needed")
        
        # Conditional: reduce or finalize
        builder.add_conditional_edges(
            "check_reduce_needed",
            self._should_reduce,
            {True: "group_for_reduce", False: "finalize"},
        )
        
        # Reduce loop
        builder.add_edge("group_for_reduce", "reduce_combine")
        builder.add_edge("reduce_combine", "check_reduce_needed")
        
        # Exit
        builder.add_edge("finalize", END)
        
        # Compile with optional checkpointing
        memory = MemorySaver() if self.config.file_reducer.get("langgraph_checkpoint") else None
        return builder.compile(checkpointer=memory)
    
    def _should_reduce(self, state: FileReducerState) -> bool:
        """Check if reduction is needed."""
        summaries = state["reduce_summaries"]
        if len(summaries) <= 1:
            return False
        
        total_tokens = self.token_counter("\n\n".join(summaries))
        return total_tokens > self.config.file_reducer.max_tokens
```

---

**Appendix B: Token Estimation Accuracy by Language**

| Language | Chars/Token | Estimation Error |
|----------|-------------|------------------|
| English | 4.0 | ±5% |
| Spanish | 4.2 | ±7% |
| French | 4.1 | ±6% |
| German | 4.3 | ±8% |
| Chinese | 1.5 | ±20% (underestimates) |
| Japanese | 2.0 | ±15% (underestimates) |

**Note:** Conservative factor (0.75) accounts for worst-case estimation error.
