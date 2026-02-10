# Testing Patterns

**Analysis Date:** 2026-02-10

## Test Framework

**Runner:**
- pytest 8.4.1+
- Configuration: `pytest.ini` in repository root

**Assertion Library:**
- Python `assert` statements (no external library)
- httpx for HTTP assertions in integration tests

**Run Commands:**
```bash
uv run pytest                                    # Run all tests
uv run pytest -k "test_chunk"                  # Run tests matching pattern
uv run pytest openrag/components/indexer/chunker/test_chunking.py  # Single file
uv run pytest --markers integration            # Run integration tests
uv run pytest --markers unit                   # Run unit tests
```

**pytest Configuration (`pytest.ini`):**
- Test paths: `testpaths = openrag`
- Python path: `pythonpath = ./openrag`
- Test discovery: `python_files = test_*.py`
- Environment variables set:
  - `CONFIG_PATH=./.hydra_config`
  - `PROMPTS_DIR=./prompts/example1`
  - `LOG_DIR=./logs`
- Test markers: `integration`, `unit`

## Test File Organization

**Location:**
- Unit tests: Co-located with source code
  - `openrag/components/indexer/chunker/test_chunking.py` (next to business logic)
  - `openrag/utils/test_logger.py`
  - `openrag/components/indexer/loaders/test_media_loader.py`
- Integration tests: `tests/api_tests/` directory (separate from source)
  - `tests/api_tests/test_indexer.py`
  - `tests/api_tests/test_health.py`
  - `tests/api_tests/test_openai_compat.py`
  - `tests/api_tests/conftest.py` (shared fixtures)
- Test resources: `tests/resources/` directory
- Robot Framework tests: `tests/api/` (separate test framework)

**Naming:**
- Test files: `test_*.py` (prefix)
- Test classes: `Test<Feature>` (e.g., `TestSplitMdElements`, `TestFileIndexing`)
- Test methods: `test_<scenario>` (e.g., `test_simple_text_only`, `test_file_upload_with_metadata`)

**Structure:**
```
openrag/
├── components/
│   ├── indexer/
│   │   ├── chunker/
│   │   │   ├── utils.py
│   │   │   └── test_chunking.py      # Unit test co-located
│   │   ├── loaders/
│   │   │   └── test_media_loader.py  # Unit test co-located
│   │   └── utils/
│   │       ├── text_sanitizer.py
│   │       └── test_text_sanitizer.py
│   └── (other components)
├── utils/
│   └── test_logger.py                # Unit test co-located
└── (other packages)

tests/
├── api_tests/
│   ├── conftest.py                   # Shared fixtures
│   ├── test_indexer.py               # Integration tests
│   ├── test_health.py
│   ├── test_openai_compat.py
│   └── (other integration tests)
├── resources/
│   └── test_file.pdf
└── api/
    └── *.robot                       # Robot Framework tests
```

## Test Structure

**Suite Organization:**
Class-based test organization with one test class per feature:

```python
class TestSplitMdElements:
    """Test suite for split_md_elements function."""

    def test_simple_text_only(self):
        """Test parsing markdown with only text content."""
        md_text = "This is a simple paragraph.\n\nAnother paragraph here."
        elements = split_md_elements(md_text)

        assert len(elements) == 1
        assert elements[0].type == "text"
        assert md_text == elements[0].content

    def test_single_table(self):
        """Test parsing a single markdown table."""
        md_text = "Some text before.\n\n| Header 1 | Header 2 |..."
        elements = split_md_elements(md_text)

        assert len(elements) == 3
        assert elements[0].type == "text"
        assert elements[1].type == "table"
```

**Patterns:**

Setup:
- No explicit `setUp()` methods (pytest uses fixtures instead)
- Fixture pattern for test data: `@pytest.fixture` decorators in conftest.py
- Example fixture:
```python
@pytest.fixture
def sample_text_file(tmp_path):
    """Create a sample text file for upload tests."""
    content = """This is a test document..."""
    file_path = tmp_path / "test_doc.txt"
    file_path.write_text(content)
    return file_path
```

Teardown:
- Fixtures use `yield` for setup/cleanup:
```python
@pytest.fixture
def created_partition(api_client, test_partition_name):
    """Create a partition and clean it up after the test."""
    response = api_client.post(f"/partition/{test_partition_name}")
    assert response.status_code in [200, 201]
    yield test_partition_name
    # Cleanup
    try:
        api_client.delete(f"/partition/{test_partition_name}")
    except Exception:
        pass
```

Assertions:
- Direct `assert` statements with context
- Descriptive assertion messages:
```python
assert response.status_code in [200, 201], f"Failed to create partition: {response.text}"
assert "Header 1" in elements[1].content
assert len(table_elements) == 1
```

## Mocking

**Framework:** pytest built-in mocking or manual test doubles

**Patterns:**
- Manual mock methods in test classes:
```python
def mock_length_function(self, text):
    """Mock function that estimates token count (~4 chars per token)."""
    return len(text) // 4
```

- Manual test data creation (no external mocking library required):
```python
@pytest.fixture
def sample_markdown_with_image(tmp_path):
    """Create markdown file with embedded data URI image."""
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    content = f"# Test Document\n\n![sample image]({data_uri})\n\nTest content."
    file_path = tmp_path / "test_with_image.md"
    file_path.write_text(content)
    return file_path
```

**What to Mock:**
- External API calls (VLLM embeddings for CI tests) → Mock VLM in `.github/workflows/api_tests/mock_vllm.py`
- File I/O (use `tmp_path` fixture instead)
- HTTP requests in integration tests (use real httpx client connecting to running server)

**What NOT to Mock:**
- Core business logic (sanitization, chunking, parsing)
- Database operations (use real Milvus in test environment)
- Ray actor calls (when testing Ray integration tests)

## Fixtures and Factories

**Test Data:**
Custom fixture functions that create structured test data:

```python
@pytest.fixture(scope="session")
def api_client():
    """Create HTTP client for API tests."""
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client

@pytest.fixture(scope="session", autouse=True)
def wait_for_api():
    """Wait for OpenRAG API to be ready."""
    max_retries = 60
    for i in range(max_retries):
        try:
            response = httpx.get(f"{API_BASE_URL}/health_check", timeout=5.0)
            if response.status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(2)
    pytest.fail(f"API not ready after {max_retries * 2} seconds")

@pytest.fixture
def test_partition_name():
    """Generate unique partition name for test isolation."""
    return f"test-partition-{uuid.uuid4().hex[:8]}"
```

**Location:**
- Conftest fixtures: `tests/api_tests/conftest.py` (shared across integration tests)
- Inline fixtures: Defined in test files for specific tests
- Example location: `openrag/components/indexer/loaders/test_media_loader.py` defines local fixture methods

**Factory Pattern:**
- Test helper methods within test classes:
```python
def get_audio_chunks(
    self,
    sound: AudioSegment,
    max_chunk_ms: int,
    min_silence_len_ms: int,
    silence_thresh_db: int,
) -> list:
    """Reproduce chunking logic from media_loader."""
    from pydub import silence
    # Implementation...
    return chunks
```

## Coverage

**Requirements:** No coverage enforcement configured in pytest.ini

**View Coverage:**
```bash
uv run pytest --cov=openrag           # Generate coverage report (if pytest-cov installed)
```

## Test Types

**Unit Tests:**
- Scope: Single function or method in isolation
- Location: Co-located with source code (e.g., `openrag/components/indexer/chunker/test_chunking.py`)
- Examples:
  - `TestSplitMdElements` tests markdown parsing logic
  - `TestChunkTable` tests table chunking algorithm
  - `TestSanitizeText` tests text sanitization utility
- Dependencies: Minimal (no external services)
- Speed: Fast (< 100ms per test)
- Markers: `@pytest.mark.unit` (if used)

**Integration Tests:**
- Scope: API endpoints with full system
- Location: `tests/api_tests/` directory
- Examples:
  - `TestFileIndexing` tests file upload through API
  - `TestSupportedTypes` tests endpoint response
  - `TestHealthCheck` tests API readiness
- Dependencies: Running OpenRAG server required
- Speed: Slower (1-30 seconds per test)
- Setup: Fixtures handle server readiness (`wait_for_api`), partition creation/cleanup
- Markers: `@pytest.mark.integration`

**E2E Tests:**
- Framework: Robot Framework (not pytest)
- Location: `tests/api/` directory (*.robot files)
- Scope: High-level user workflows
- Not maintained in active pytest suite

## Common Patterns

**Async Testing:**
- Framework support: `pytest-asyncio` configured in `pyproject.toml`
- Pattern: Mark async tests with `@pytest.mark.asyncio`:
```python
@pytest.mark.asyncio
async def test_async_search():
    result = await vectordb.async_search(query="test")
    assert len(result) > 0
```

**Error Testing:**
- Test for specific exceptions using `pytest.raises()`:
```python
def test_invalid_input_raises_error():
    with pytest.raises(ValueError, match="Invalid input"):
        function_that_should_raise()
```

**Task Polling Pattern** (Integration tests):
```python
def wait_for_task(api_client, task_id: str, timeout: int = TASK_TIMEOUT) -> dict:
    """Wait for task completion, polling status endpoint."""
    start = time.time()
    while time.time() - start < timeout:
        response = api_client.get(f"/indexer/task/{task_id}")

        if response.status_code == 404:
            time.sleep(0.5)
            continue

        if response.status_code != 200:
            raise AssertionError(f"Task status failed: {response.text}")

        status = response.json()
        state = status.get("task_state")

        if state == "COMPLETED":
            return status
        elif state == "FAILED":
            raise AssertionError(f"Task failed: {status}")

        time.sleep(1)

    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")
```

---

*Testing analysis: 2026-02-10*
