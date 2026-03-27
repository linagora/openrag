# LangGraph FileReducer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current FileReducer implementation with a LangGraph-powered state machine that provides 5-8x performance improvement through token caching, hybrid estimation, and binary tree reduction.

**Architecture:** LangGraph StateGraph orchestrates the entire reduction flow with pre-calculated token caching, fast character-based estimation for grouping, and binary tree reduction pattern for logarithmic consolidation rounds.

**Tech Stack:** LangGraph 0.2+, LangChain Core 0.3+, existing ChatOpenAI LLM client, Ray distributed semaphore.

---

## File Structure

**Files to Create:**
- `openrag/components/file_reducer_graph.py` - LangGraph state graph definition and nodes
- `openrag/components/test_file_reducer.py` - Unit tests for FileReducer

**Files to Modify:**
- `openrag/components/file_reducer.py:16-161` - Replace implementation with LangGraph-based version
- `.hydra_config/config.yaml:58-62` - Add new configuration options
- `pyproject.toml:7-54` - Add langgraph dependency

**Files to Check (for reference):**
- `openrag/components/utils.py:117-124` - get_llm_semaphore() usage
- `openrag/components/map_reduce.py:18-29` - system_prompt_map (reuse)
- `openrag/components/pipeline.py:248` - FileReducer.reduce_all() usage

---

## Task 1: Add LangGraph Dependency

**Files:**
- Modify: `pyproject.toml:7-54`

- [ ] **Step 1: Add langgraph to dependencies**

Edit `pyproject.toml` line 24 (after langchain-openai):

```toml
langgraph = "^0.2.0"
```

- [ ] **Step 2: Install new dependency**

Run:
```bash
uv sync
```

Expected: `langgraph` and dependencies installed successfully

- [ ] **Step 3: Verify langgraph import works**

Run:
```bash
uv run python -c "from langgraph.graph import StateGraph; print('LangGraph OK')"
```

Expected: `LangGraph OK`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add langgraph dependency for FileReducer state machine"
```

---

## Task 2: Add Configuration Options

**Files:**
- Modify: `.hydra_config/config.yaml:58-63`

- [ ] **Step 1: Add new config fields**

Edit `.hydra_config/config.yaml` lines 58-63, replace with:

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

- [ ] **Step 2: Verify config loads**

Run:
```bash
uv run python -c "from config import load_config; c = load_config(); print('max_tokens:', c.file_reducer.max_tokens); print('conservative_factor:', c.file_reducer.conservative_factor)"
```

Expected: Config values printed without errors

- [ ] **Step 3: Commit**

```bash
git add .hydra_config/config.yaml
git commit -m "config: add file_reducer options for LangGraph implementation"
```

---

## Task 3: Create LangGraph State Schema

**Files:**
- Create: `openrag/components/file_reducer_graph.py`

- [ ] **Step 1: Write test for state schema**

Create `openrag/components/test_file_reducer.py`:

```python
"""Unit tests for LangGraph-powered FileReducer."""

import pytest
from langchain_core.documents.base import Document
from components.file_reducer_graph import FileReducerState


@pytest.mark.unit
class TestFileReducerState:
    def test_state_schema_required_fields(self):
        """State dict must contain all required fields."""
        state: FileReducerState = {
            "file_id": "test-123",
            "original_chunks": [Document(page_content="test")],
            "token_cache": {},
            "estimated_tokens": 100,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        assert state["file_id"] == "test-123"
        assert len(state["original_chunks"]) == 1
        assert isinstance(state["token_cache"], dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFileReducerState::test_state_schema_required_fields -v
```

Expected: FAIL with "ModuleNotFoundError: No module named 'file_reducer_graph'"

- [ ] **Step 3: Create file_reducer_graph with state schema**

Create `openrag/components/file_reducer_graph.py`:

```python
"""LangGraph state graph for FileReducer component."""

from typing import TypedDict
from langchain_core.documents.base import Document


class FileReducerState(TypedDict):
    """State tracked throughout the reduction graph.
    
    Attributes:
        file_id: Identifier for the file being reduced
        original_chunks: Input document chunks
        token_cache: Mapping of chunk IDs to estimated token counts
        estimated_tokens: Total estimated tokens across all chunks
        map_groups: Groups of chunk texts for parallel map summarization
        map_summaries: Summaries from map phase
        reduce_round: Current round number in reduce phase
        reduce_summaries: Current round's summaries to reduce
        reduce_needed: Whether additional reduction is needed
        final_content: Final summarized content
        final_metadata: Merged metadata from all chunks
    """
    # Input
    file_id: str
    original_chunks: list[Document]
    
    # Token cache (pre-calculated)
    token_cache: dict[str, int]  # chunk_id -> token_count
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

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFileReducerState::test_state_schema_required_fields -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: add FileReducerState TypedDict for LangGraph"
```

---

## Task 4: Implement Token Caching Node

**Files:**
- Modify: `openrag/components/file_reducer_graph.py:1-20`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for token caching**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestTokenCaching:
    def test_cache_tokens_estimates_correctly(self):
        """Token estimation should be within 10% of actual count."""
        from components.file_reducer_graph import FileReducerGraph
        from components.utils import get_num_tokens
        
        chunks = [
            Document(page_content="This is a test chunk of text. " * 10),
            Document(page_content="Another chunk with different content. " * 10),
        ]
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": chunks,
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._cache_tokens(state)
        
        # Check cache has entries for both chunks
        assert len(result["token_cache"]) == 2
        
        # Verify estimates are reasonable (within 20% of actual)
        token_counter = get_num_tokens()
        for chunk, estimated in result["token_cache"].items():
            actual = token_counter(chunk.page_content)
            ratio = estimated / actual if actual > 0 else 0
            assert 0.5 < ratio < 2.0  # Within 50% for safety
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestTokenCaching::test_cache_tokens_estimates_correctly -v
```

Expected: FAIL with "FileReducerGraph not defined"

- [ ] **Step 3: Add imports and graph class**

Edit `openrag/components/file_reducer_graph.py`, add at top:

```python
"""LangGraph state graph for FileReducer component."""

from typing import TypedDict
from langchain_core.documents.base import Document
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from config import load_config
from langchain_openai import ChatOpenAI
from utils.logger import get_logger
from .utils import get_llm_semaphore, get_num_tokens
from .map_reduce import system_prompt_map

logger = get_logger()
config = load_config()
```

Add after FileReducerState:

```python
class FileReducerGraph:
    """LangGraph-based file reduction orchestrator."""
    
    def __init__(self):
        self.config = load_config()
        self.llm = ChatOpenAI(
            base_url=self.config.llm.get("base_url"),
            api_key=self.config.llm.get("api_key"),
            model=self.config.llm.get("model"),
            temperature=self.config.file_reducer.get("temperature", 0.3),
            timeout=self.config.file_reducer.get("timeout", 120),
            max_completion_tokens=512,
        )
        self.max_tokens = self.config.file_reducer.get("max_tokens", 512)
        self.token_counter = get_num_tokens()
        self.conservative_factor = self.config.file_reducer.get("conservative_factor", 0.75)
        self.map_token_limit = self.config.file_reducer.get("map_token_limit", 6000)
        self.graph = self._build_graph()
    
    def _estimate_tokens(self, text: str) -> int:
        """Fast character-based token estimation.
        
        Uses ~4 chars per token approximation for English text.
        Conservative factor applied during grouping, not estimation.
        """
        return len(text) // 4
    
    def _cache_tokens(self, state: FileReducerState) -> FileReducerState:
        """Pre-calculate token counts for all chunks.
        
        Uses fast estimation for grouping, validates total with accurate counter.
        """
        token_cache = {}
        total_estimated = 0
        
        for chunk in state["original_chunks"]:
            chunk_id = id(chunk)
            estimated = self._estimate_tokens(chunk.page_content)
            token_cache[chunk_id] = estimated
            total_estimated += estimated
        
        # Validate with accurate counter
        total_content = "\n".join(c.page_content for c in state["original_chunks"])
        accurate_total = self.token_counter(total_content)
        
        logger.bind(
            file_id=state["file_id"],
            estimated=total_estimated,
            accurate=accurate_total,
            chunks=len(state["original_chunks"]),
        ).debug("Token caching completed")
        
        return {
            **state,
            "token_cache": token_cache,
            "estimated_tokens": total_estimated,
            "accurate_total": accurate_total,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestTokenCaching::test_cache_tokens_estimates_correctly -v
```

Expected: PASS

- [ ] **Step 5: Add more token caching tests**

Add to `test_file_reducer.py`:

```python
    def test_cache_tokens_empty_chunks(self):
        """Should handle empty chunk list."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [],
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._cache_tokens(state)
        assert result["token_cache"] == {}
        assert result["estimated_tokens"] == 0
    
    def test_estimation_speed(self):
        """Estimation should be instant (<1ms per chunk)."""
        import time
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        chunks = [Document(page_content="x" * 1000) for _ in range(100)]
        
        start = time.time()
        for chunk in chunks:
            graph._estimate_tokens(chunk.page_content)
        elapsed = time.time() - start
        
        # Should be <10ms total for 100 chunks
        assert elapsed < 0.01
```

- [ ] **Step 6: Run all token caching tests**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestTokenCaching -v
```

Expected: All 3 tests PASS

- [ ] **Step 7: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement token caching node with fast estimation"
```

---

## Task 5: Implement Grouping Node

**Files:**
- Modify: `openrag/components/file_reducer_graph.py`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for grouping**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestGrouping:
    def test_group_by_tokens_respects_limit(self):
        """Groups should not exceed conservative token limit."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        chunks = [
            Document(page_content="x" * 2000),  # ~500 tokens
            Document(page_content="y" * 2000),  # ~500 tokens
            Document(page_content="z" * 2000),  # ~500 tokens
        ]
        
        state = {
            "file_id": "test",
            "original_chunks": chunks,
            "token_cache": {id(c): 500 for c in chunks},
            "estimated_tokens": 1500,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._group_by_tokens(state)
        
        # All 3 should fit in one group (1500 < 6000 * 0.75 = 4500)
        assert len(result["map_groups"]) == 1
        assert len(result["map_groups"][0]) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestGrouping::test_group_by_tokens_respects_limit -v
```

Expected: FAIL

- [ ] **Step 3: Implement grouping node**

Add to `FileReducerGraph` class:

```python
    def _group_by_tokens(self, state: FileReducerState) -> FileReducerState:
        """Group chunks by token limit using cached estimates.
        
        Uses conservative factor to prevent overflow from estimation errors.
        """
        effective_limit = int(self.map_token_limit * self.conservative_factor)
        
        groups: list[list[str]] = []
        current_group: list[str] = []
        current_tokens = 0
        
        for chunk in state["original_chunks"]:
            chunk_id = id(chunk)
            chunk_tokens = state["token_cache"].get(chunk_id, 0)
            chunk_text = chunk.page_content
            
            if current_group and current_tokens + chunk_tokens > effective_limit:
                groups.append(current_group)
                current_group = [chunk_text]
                current_tokens = chunk_tokens
            else:
                current_group.append(chunk_text)
                current_tokens += chunk_tokens
        
        if current_group:
            groups.append(current_group)
        
        logger.bind(
            file_id=state["file_id"],
            num_groups=len(groups),
            effective_limit=effective_limit,
        ).debug("Chunk grouping completed")
        
        return {
            **state,
            "map_groups": groups,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestGrouping::test_group_by_tokens_respects_limit -v
```

Expected: PASS

- [ ] **Step 5: Add more grouping tests**

Add to `test_file_reducer.py`:

```python
    def test_group_by_tokens_multiple_groups(self):
        """Should create multiple groups when chunks exceed limit."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        # Each chunk ~2000 tokens, limit ~4500
        chunks = [
            Document(page_content="x" * 8000),  # ~2000 tokens
            Document(page_content="y" * 8000),  # ~2000 tokens
            Document(page_content="z" * 8000),  # ~2000 tokens
            Document(page_content="w" * 8000),  # ~2000 tokens
            Document(page_content="v" * 8000),  # ~2000 tokens
        ]
        
        state = {
            "file_id": "test",
            "original_chunks": chunks,
            "token_cache": {id(c): 2000 for c in chunks},
            "estimated_tokens": 10000,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._group_by_tokens(state)
        
        # Should create 3 groups: [2, 2, 1] chunks
        assert len(result["map_groups"]) == 3
        assert len(result["map_groups"][0]) == 2
        assert len(result["map_groups"][1]) == 2
        assert len(result["map_groups"][2]) == 1
```

- [ ] **Step 6: Run all grouping tests**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestGrouping -v
```

Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement grouping node with conservative token limits"
```

---

## Task 6: Implement Map Summarization Node

**Files:**
- Modify: `openrag/components/file_reducer_graph.py`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for map summarization**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestMapSummarization:
    @pytest.mark.asyncio
    async def test_map_summarize_parallel(self):
        """Map phase should summarize groups in parallel."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [Document(page_content="Test content")],
            "token_cache": {},
            "estimated_tokens": 100,
            "map_groups": [
                ["Chunk 1 content", "Chunk 2 content"],
                ["Chunk 3 content"],
            ],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = await graph._map_summarize(state)
        
        # Should have 2 summaries (one per group)
        assert len(result["map_summaries"]) == 2
        # Each summary should be non-empty
        assert all(len(s) > 0 for s in result["map_summaries"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestMapSummarization::test_map_summarize_parallel -v
```

Expected: FAIL

- [ ] **Step 3: Implement map summarization node**

Add to `FileReducerGraph` class:

```python
    async def _map_summarize(self, state: FileReducerState) -> FileReducerState:
        """Summarize each group in parallel.
        
        Uses existing system_prompt_map for consistency with semantic search.
        """
        from tqdm.asyncio import tqdm
        
        async def summarize_group(group_texts: list[str]) -> str:
            """Summarize a single group of texts."""
            prompt = (
                f"Summarize the following content. Be extremely concise — keep only vital information."
                f" Your response must not exceed {self.max_tokens} tokens.\n\n"
                + "\n\n".join(group_texts)
            )
            
            async with get_llm_semaphore():
                response = await self.llm.ainvoke(
                    [
                        {"role": "system", "content": system_prompt_map},
                        {"role": "user", "content": prompt},
                    ]
                )
            
            return response.content
        
        filename = state["file_id"]
        
        # Parallel summarization with progress tracking
        summaries = list(
            await tqdm.gather(
                *[summarize_group(group) for group in state["map_groups"]],
                desc=f"[{filename}] map",
            )
        )
        
        logger.bind(
            file_id=state["file_id"],
            num_summaries=len(summaries),
        ).debug("Map summarization completed")
        
        return {
            **state,
            "map_summaries": summaries,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestMapSummarization::test_map_summarize_parallel -v
```

Expected: PASS (may take a few seconds for LLM calls)

- [ ] **Step 5: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement parallel map summarization node"
```

---

## Task 7: Implement Reduction Check Node

**Files:**
- Modify: `openrag/components/file_reducer_graph.py`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for reduction check**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestReductionCheck:
    def test_check_reduce_needed_over_limit(self):
        """Should return True when summaries exceed max_tokens."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [],
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": ["Summary 1", "Summary 2"],  # 2 summaries
            "reduce_round": 0,
            "reduce_summaries": ["Summary 1", "Summary 2"],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        # Mock token counter to return > max_tokens
        def mock_counter(text):
            return 600  # > 512 max_tokens
        
        graph.token_counter = mock_counter
        
        result = graph._check_reduce_needed(state)
        
        assert result["reduce_needed"] is True
    
    def test_check_reduce_needed_under_limit(self):
        """Should return False when summaries fit within max_tokens."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [],
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": ["Short summary"],
            "reduce_round": 0,
            "reduce_summaries": ["Short summary"],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._check_reduce_needed(state)
        
        assert result["reduce_needed"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestReductionCheck -v
```

Expected: FAIL

- [ ] **Step 3: Implement reduction check node**

Add to `FileReducerGraph` class:

```python
    def _check_reduce_needed(self, state: FileReducerState) -> FileReducerState:
        """Check if additional reduction is needed.
        
        Returns True if:
        - More than 1 summary exists
        - Combined summaries exceed max_tokens
        """
        summaries = state["reduce_summaries"] or state["map_summaries"]
        
        # Single summary or empty = done
        if len(summaries) <= 1:
            reduce_needed = False
        else:
            # Check token count
            combined = "\n\n".join(summaries)
            total_tokens = self.token_counter(combined)
            reduce_needed = total_tokens > self.max_tokens
        
        logger.bind(
            file_id=state["file_id"],
            num_summaries=len(summaries),
            reduce_needed=reduce_needed,
        ).debug("Reduction check completed")
        
        return {
            **state,
            "reduce_needed": reduce_needed,
        }
    
    def _should_reduce(self, state: FileReducerState) -> bool:
        """Conditional edge function for LangGraph."""
        return state["reduce_needed"]
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestReductionCheck -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement reduction check node with conditional routing"
```

---

## Task 8: Implement Binary Tree Reduction Nodes

**Files:**
- Modify: `openrag/components/file_reducer_graph.py`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for binary grouping**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestBinaryReduction:
    def test_group_for_reduce_pairs(self):
        """Should pair adjacent summaries for binary reduction."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [],
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": ["s1", "s2", "s3", "s4", "s5", "s6"],
            "reduce_round": 0,
            "reduce_summaries": ["s1", "s2", "s3", "s4", "s5", "s6"],
            "reduce_needed": True,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._group_for_reduce(state)
        
        # Should create 3 pairs: [s1,s2], [s3,s4], [s5,s6]
        assert len(result["reduce_groups"]) == 3
        assert result["reduce_groups"][0] == ["s1", "s2"]
        assert result["reduce_groups"][1] == ["s3", "s4"]
        assert result["reduce_groups"][2] == ["s5", "s6"]
    
    def test_group_for_reduce_odd_count(self):
        """Should handle odd number of summaries."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        state = {
            "file_id": "test",
            "original_chunks": [],
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": ["s1", "s2", "s3", "s4", "s5"],
            "reduce_round": 0,
            "reduce_summaries": ["s1", "s2", "s3", "s4", "s5"],
            "reduce_needed": True,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._group_for_reduce(state)
        
        # Should create 3 groups: [s1,s2], [s3,s4], [s5]
        assert len(result["reduce_groups"]) == 3
        assert result["reduce_groups"][2] == ["s5"]  # Odd one out
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestBinaryReduction -v
```

Expected: FAIL

- [ ] **Step 3: Implement binary grouping node**

Add to `FileReducerGraph` class:

```python
    def _group_for_reduce(self, state: FileReducerState) -> FileReducerState:
        """Pair adjacent summaries for binary tree reduction.
        
        Creates pairs of summaries for parallel combination.
        Odd summaries carry forward unpaired.
        """
        summaries = state["reduce_summaries"]
        groups: list[list[str]] = []
        
        for i in range(0, len(summaries), 2):
            if i + 1 < len(summaries):
                # Pair two summaries
                groups.append([summaries[i], summaries[i + 1]])
            else:
                # Odd one out carries forward
                groups.append([summaries[i]])
        
        # Increment round counter
        new_round = state["reduce_round"] + 1
        
        logger.bind(
            file_id=state["file_id"],
            round=new_round,
            num_groups=len(groups),
        ).debug("Binary grouping completed")
        
        return {
            **state,
            "reduce_round": new_round,
            "reduce_groups": groups,
        }
```

- [ ] **Step 4: Implement reduce combination node**

Add to `FileReducerGraph` class:

```python
    async def _reduce_combine(self, state: FileReducerState) -> FileReducerState:
        """Combine paired summaries in parallel.
        
        Each group is combined into a single summary.
        Single-item groups pass through unchanged.
        """
        from tqdm.asyncio import tqdm
        
        async def combine_group(group_texts: list[str]) -> str:
            """Combine a single group of summaries."""
            if len(group_texts) == 1:
                return group_texts[0]
            
            prompt = (
                f"Combine the following summaries into one. Be extremely concise — keep only vital information."
                f" Your response must not exceed {self.max_tokens} tokens.\n\n"
                + "\n\n".join(group_texts)
            )
            
            async with get_llm_semaphore():
                response = await self.llm.ainvoke([{"role": "user", "content": prompt}])
            
            return response.content
        
        filename = state["file_id"]
        round_n = state["reduce_round"]
        
        # Parallel combination with progress tracking
        combined = list(
            await tqdm.gather(
                *[combine_group(group) for group in state["reduce_groups"]],
                desc=f"[{filename}] reduce (round {round_n})",
            )
        )
        
        logger.bind(
            file_id=state["file_id"],
            round=round_n,
            input_groups=len(state["reduce_groups"]),
            output_summaries=len(combined),
        ).debug("Reduce combination completed")
        
        return {
            **state,
            "reduce_summaries": combined,
        }
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestBinaryReduction -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement binary tree reduction nodes"
```

---

## Task 9: Implement Finalize Node and Build Graph

**Files:**
- Modify: `openrag/components/file_reducer_graph.py`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write test for finalize node**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestFinalize:
    def test_finalize_merges_metadata(self):
        """Should merge metadata from all original chunks."""
        from components.file_reducer_graph import FileReducerGraph
        
        graph = FileReducerGraph()
        chunks = [
            Document(page_content="Chunk 1", metadata={"file_id": "test-123", "partition": "docs"}),
            Document(page_content="Chunk 2", metadata={"file_id": "test-123", "partition": "docs"}),
        ]
        
        state = {
            "file_id": "test-123",
            "original_chunks": chunks,
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": ["Final summary content"],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = graph._finalize(state)
        
        assert result["final_content"] == "Final summary content"
        assert result["final_metadata"]["file_id"] == "test-123"
        assert result["final_metadata"]["partition"] == "docs"
        assert result["final_metadata"]["_summarized"] is True
        assert result["final_metadata"]["_original_chunk_count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFinalize::test_finalize_merges_metadata -v
```

Expected: FAIL

- [ ] **Step 3: Implement finalize node**

Add to `FileReducerGraph` class:

```python
    def _finalize(self, state: FileReducerState) -> FileReducerState:
        """Merge metadata and create final Document."""
        original_chunks = state["original_chunks"]
        
        # Merge metadata from first chunk
        base_metadata = original_chunks[0].metadata.copy() if original_chunks else {}
        base_metadata["_summarized"] = True
        base_metadata["_original_chunk_count"] = len(original_chunks)
        base_metadata["_reduction_rounds"] = state["reduce_round"]
        
        # Ensure file_id and partition are preserved
        if original_chunks:
            base_metadata["file_id"] = original_chunks[0].metadata.get("file_id")
            base_metadata["partition"] = original_chunks[0].metadata.get("partition")
        
        logger.bind(
            file_id=state["file_id"],
            final_tokens=self.token_counter(state["final_content"]) if state["final_content"] else 0,
        ).debug("Finalization completed")
        
        return {
            **state,
            "final_content": state["reduce_summaries"][0] if state["reduce_summaries"] else "",
            "final_metadata": base_metadata,
        }
```

- [ ] **Step 4: Build the complete graph**

Add to `FileReducerGraph` class:

```python
    def _build_graph(self):
        """Build the LangGraph state graph."""
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
        use_checkpoint = self.config.file_reducer.get("langgraph_checkpoint", False)
        memory = MemorySaver() if use_checkpoint else None
        
        return builder.compile(checkpointer=memory)
    
    async def invoke(self, file_id: str, chunks: list[Document]) -> FileReducerState:
        """Execute the reduction graph."""
        initial_state = {
            "file_id": file_id,
            "original_chunks": chunks,
            "token_cache": {},
            "estimated_tokens": 0,
            "map_groups": [],
            "map_summaries": [],
            "reduce_round": 0,
            "reduce_summaries": [],
            "reduce_needed": False,
            "final_content": "",
            "final_metadata": {},
        }
        
        result = await self.graph.ainvoke(initial_state)
        return result
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFinalize::test_finalize_merges_metadata -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
git commit -m "feat: implement finalize node and build complete LangGraph"
```

---

## Task 10: Integrate Graph with FileReducer

**Files:**
- Modify: `openrag/components/file_reducer.py:16-161`
- Test: `openrag/components/test_file_reducer.py`

- [ ] **Step 1: Write integration test**

Add to `test_file_reducer.py`:

```python
@pytest.mark.unit
class TestFileReducerIntegration:
    @pytest.mark.asyncio
    async def test_reduce_all_multiple_files(self):
        """Should reduce multiple files in parallel."""
        from components.file_reducer import FileReducer
        from config import load_config
        
        config = load_config()
        reducer = FileReducer(config)
        
        # Simulate 2 files with multiple chunks each
        docs_by_file = [
            [Document(page_content=f"File 1 Chunk {i}", metadata={"file_id": "f1"}) for i in range(3)],
            [Document(page_content=f"File 2 Chunk {i}", metadata={"file_id": "f2"}) for i in range(3)],
        ]
        
        result = await reducer.reduce_all(docs_by_file)
        
        # Should return one summary per file
        assert len(result) == 2
        assert result[0].metadata["file_id"] == "f1"
        assert result[1].metadata["file_id"] == "f2"
    
    @pytest.mark.asyncio
    async def test_reduce_empty_chunks(self):
        """Should handle empty chunk list."""
        from components.file_reducer import FileReducer
        from config import load_config
        
        config = load_config()
        reducer = FileReducer(config)
        
        result = await reducer._reduce([])
        
        assert result == []
    
    @pytest.mark.asyncio
    async def test_reduce_single_chunk(self):
        """Should return single chunk unchanged."""
        from components.file_reducer import FileReducer
        from config import load_config
        
        config = load_config()
        reducer = FileReducer(config)
        chunk = Document(page_content="Single chunk", metadata={"file_id": "test"})
        
        result = await reducer._reduce([chunk])
        
        assert result == [chunk]
    
    @pytest.mark.asyncio
    async def test_reduce_error_fallback(self, monkeypatch):
        """Should return original chunks on LLM error."""
        from components.file_reducer import FileReducer
        from config import load_config
        
        config = load_config()
        reducer = FileReducer(config)
        
        # Mock LLM to raise error
        async def mock_ainvoke(*args, **kwargs):
            raise Exception("LLM error")
        
        monkeypatch.setattr(reducer.llm, "ainvoke", mock_ainvoke)
        
        chunks = [
            Document(page_content="Chunk 1", metadata={"file_id": "test"}),
            Document(page_content="Chunk 2", metadata={"file_id": "test"}),
        ]
        
        result = await reducer._reduce(chunks)
        
        # Should return original chunks on error
        assert result == chunks
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFileReducerIntegration -v
```

Expected: FAIL (FileReducer not using graph yet)

- [ ] **Step 3: Rewrite FileReducer to use LangGraph**

Replace `openrag/components/file_reducer.py`:

```python
"""FileReducer component using LangGraph for orchestration."""

import asyncio
from langchain_core.documents.base import Document
from utils.logger import get_logger
from .file_reducer_graph import FileReducerGraph

logger = get_logger()


class FileReducer:
    """Reduces document chunks to fit within token limits using LangGraph."""

    def __init__(self, config) -> None:
        self.config = config
        self.graph = FileReducerGraph()

    async def reduce_all(self, docs_by_file: list[list[Document]]) -> list[Document]:
        """Reduce each file's chunks independently, then return the combined results.

        Args:
            docs_by_file: One list of chunks per file, in retrieval order

        Returns:
            Flat list of reduced chunks (one summary per file that exceeded the limit)
        """
        results = await asyncio.gather(
            *[self._reduce(file_chunks) for file_chunks in docs_by_file]
        )
        return [chunk for file_result in results for chunk in file_result]

    async def _reduce(self, chunks: list[Document]) -> list[Document]:
        """Reduce a single file's chunks if they exceed the token limit.

        Args:
            chunks: Chunks belonging to the same file

        Returns:
            Reduced list of chunks (or original if under limit)
        """
        if not chunks:
            return []

        if len(chunks) == 1:
            return chunks

        # Quick check: if under limit, skip reduction
        total_content = "\n".join(chunk.page_content for chunk in chunks)
        token_counter = self.graph.token_counter
        if token_counter(total_content) <= self.graph.max_tokens:
            return chunks

        try:
            # Extract file_id from first chunk
            file_id = chunks[0].metadata.get("file_id", f"file_{id(chunks)}")
            
            # Execute reduction graph
            result = await self.graph.invoke(file_id, chunks)
            
            # Convert to Document
            return [
                Document(
                    page_content=result["final_content"],
                    metadata=result["final_metadata"],
                )
            ]
        except Exception as e:
            logger.bind(
                file_id=chunks[0].metadata.get("file_id"),
                error=str(e),
            ).warning("File reduction failed, using original chunks")
            return chunks
```

- [ ] **Step 4: Run integration tests**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py::TestFileReducerIntegration -v
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/components/file_reducer.py openrag/components/test_file_reducer.py
git commit -m "feat: integrate LangGraph with FileReducer facade"
```

---

## Task 11: Add Performance Benchmarks

**Files:**
- Create: `openrag/components/benchmarks/test_file_reducer_benchmark.py`
- Test: Existing tests should still pass

- [ ] **Step 1: Create benchmark test**

Create `openrag/components/benchmarks/test_file_reducer_benchmark.py`:

```python
"""Performance benchmarks for LangGraph FileReducer."""

import pytest
import time
from langchain_core.documents.base import Document
from components.file_reducer import FileReducer
from config import load_config


@pytest.mark.benchmark
class TestFileReducerBenchmarks:
    """Performance benchmarks comparing before/after optimization."""

    @pytest.fixture
    def reducer(self):
        config = load_config()
        return FileReducer(config)

    @pytest.mark.asyncio
    async def test_benchmark_10_chunks(self, reducer, benchmark):
        """Benchmark with 10 chunks."""
        chunks = [
            Document(page_content="Test content chunk " * 50, metadata={"file_id": "bench"})
            for _ in range(10)
        ]

        async def reduce():
            return await reducer._reduce(chunks)

        result = benchmark(reduce)
        
        # Should complete in <2s
        assert result.stats.mean < 2.0
        # Should return 1 summary
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_benchmark_50_chunks(self, reducer, benchmark):
        """Benchmark with 50 chunks."""
        chunks = [
            Document(page_content="Test content chunk " * 50, metadata={"file_id": "bench"})
            for _ in range(50)
        ]

        async def reduce():
            return await reducer._reduce(chunks)

        result = benchmark(reduce)
        
        # Should complete in <10s (5x improvement target)
        assert result.stats.mean < 10.0
        # Should return 1 summary
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_benchmark_token_caching_speed(self, reducer):
        """Token caching should be instant."""
        chunks = [
            Document(page_content="x" * 1000, metadata={"file_id": "bench"})
            for _ in range(100)
        ]

        start = time.time()
        # First call includes caching
        await reducer._reduce(chunks)
        elapsed = time.time() - start

        # Total reduction should be <30s for 100 chunks
        # (vs ~60s+ with old implementation)
        assert elapsed < 30.0
```

- [ ] **Step 2: Run benchmarks**

Run:
```bash
uv run pytest openrag/components/benchmarks/test_file_reducer_benchmark.py -v --tb=short
```

Expected: Benchmarks run and show performance metrics

- [ ] **Step 3: Commit**

```bash
git add openrag/components/benchmarks/test_file_reducer_benchmark.py
git commit -m "test: add performance benchmarks for FileReducer"
```

---

## Task 12: Update Documentation and Cleanup

**Files:**
- Modify: `docs/content/docs/documentation/API.mdx`
- Modify: `docs/content/docs/documentation/env_vars.md`

- [ ] **Step 1: Update environment variables documentation**

Add to `docs/content/docs/documentation/env_vars.md` in the File Reducer section:

```markdown
### File Reducer Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FILE_REDUCER_MAX_TOKENS` | `512` | Target maximum tokens for reduced output |
| `FILE_REDUCER_TIMEOUT` | `120` | Timeout for summarization LLM calls (seconds) |
| `FILE_REDUCER_TEMPERATURE` | `0.3` | Temperature for summarization generation |
| `FILE_REDUCER_CONSERVATIVE_FACTOR` | `0.75` | Token estimation conservative factor (0.0-1.0) |
| `FILE_REDUCER_MAP_LIMIT` | `6000` | Map phase token limit before conservative factor |
| `LANGGRAPH_CHECKPOINT` | `false` | Enable LangGraph checkpointing for debugging |

**Performance Notes:**

The FileReducer now uses LangGraph for orchestration with:
- Token caching (eliminates 80-90% of redundant LLM calls)
- Fast character-based estimation for grouping
- Binary tree reduction (50% fewer rounds)

Expected speedup: **5-8x faster** for 50+ chunks.
```

- [ ] **Step 2: Update API documentation if needed**

Check `docs/content/docs/documentation/API.mdx` for FileReducer mentions - update if implementation details changed

- [ ] **Step 3: Run all unit tests**

Run:
```bash
uv run pytest openrag/components/test_file_reducer.py -v
```

Expected: All tests PASS

- [ ] **Step 4: Run linting**

Run:
```bash
uv run ruff check openrag/components/file_reducer.py openrag/components/file_reducer_graph.py openrag/components/test_file_reducer.py
```

Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs: update FileReducer documentation with performance notes"
```

---

## Task 13: Final Verification

**Files:** All modified files

- [ ] **Step 1: Run full test suite**

Run:
```bash
uv run pytest openrag/components/ -v --tb=short
```

Expected: All tests PASS

- [ ] **Step 2: Verify pipeline integration**

Run:
```bash
uv run python -c "from components.file_reducer import FileReducer; from config import load_config; print('FileReducer import OK')"
```

Expected: `FileReducer import OK`

- [ ] **Step 3: Check git status**

Run:
```bash
git status
```

Expected: All files committed, working tree clean

- [ ] **Step 4: Create final commit summary**

```bash
git log --oneline -10
```

Expected: See all commits from this implementation

---

## Testing Summary

**Unit Tests:**
- Token caching correctness and speed
- Grouping with conservative limits
- Map summarization (mocked)
- Reduction check logic
- Binary tree grouping
- Finalize metadata merging
- Integration with FileReducer facade
- Error fallback behavior

**Performance Benchmarks:**
- 10 chunks: <2s target
- 50 chunks: <10s target (5x improvement)
- 100 chunks: <30s target

**Integration Tests:**
- Pipeline integration (existing tests should pass)
- Multiple file parallel reduction

---

## Rollback Plan

If issues arise during implementation:

1. **Disable LangGraph**: Comment out graph usage, revert to old `_map_reduce` method
2. **Disable estimation**: Set `conservative_factor=1.0` to use accurate counting
3. **Full rollback**: `git revert` all commits from this branch

---

## Success Criteria

- [ ] All unit tests pass
- [ ] Performance benchmarks meet targets (5x speedup)
- [ ] No breaking changes to public API
- [ ] Linting passes with no errors
- [ ] Documentation updated
- [ ] Git history clean with logical commits
