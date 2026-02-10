# Coding Conventions

**Analysis Date:** 2026-02-10

## Naming Patterns

**Files:**
- Lowercase with underscores: `vectordb.py`, `base_loader.py`, `ray_utils.py`
- Test files: `test_*.py` (prefix-based convention)
- Loader files: `base.py`, `pdf_loaders/`, `txt_loader.py` (domain-specific directories)
- Router files: `openai.py`, `indexer.py`, `search.py`, `partition.py` (feature-based naming in `openrag/routers/`)
- Exception files: `base.py`, `vectordb.py`, `embeddings.py` (in `openrag/utils/exceptions/`)

**Functions:**
- camelCase or snake_case used contextually
- Private functions: prefixed with underscore `_get_audio_chunks()`, `_serialize_document()`
- Async functions: follow same naming as sync: `aload_document()`, `async_search()`, `async_add_documents()`
- Pure utility functions: snake_case: `sanitize_text()`, `build_url()`, `get_logger()`

**Variables:**
- snake_case: `test_partition_name`, `max_chunk_ms`, `sample_text_file`
- Constants: UPPER_CASE: `ACCEPTED_FILE_FORMATS`, `FORBIDDEN_CHARS_IN_FILE_ID`, `LOG_FILE`, `TASK_TIMEOUT`, `API_BASE_URL`
- Private attributes: prefixed with underscore: `self._vlm_endpoint`, `self._config`
- Config objects: prefix with `config.` notation: `config.loader["image_captioning"]`, `config.paths.data_dir`

**Types:**
- PascalCase for classes: `BaseLoader`, `RagPipeline`, `RetrieverPipeline`, `BaseVectorDB`
- Enum names: PascalCase with UPPER_CASE values: `class RAGMODE(Enum):` with `SIMPLERAG = "SimpleRag"`, `CHATBOTRAG = "ChatBotRag"`
- Exception classes: PascalCase: `OpenRAGError`, `VDBConnectionError`, `VDBInsertError`, `EmbeddingError`
- Test class names: `Test<Feature>`: `TestSplitMdElements`, `TestChunkTable`, `TestAudioSegmentOperations`, `TestFileIndexing`

## Code Style

**Formatting:**
- Line length: 120 characters (configured in `pyproject.toml`: `line-length = 120`)
- Python version: 3.12+ required (in `pyproject.toml`: `requires-python = ">=3.12"`)
- No specific formatter configured; ruff used for linting

**Linting:**
- Tool: Ruff 0.14.1+
- Configuration: `pyproject.toml` in `[tool.ruff]` section
- Key rules enabled:
  - E, W: pycodestyle errors and warnings
  - F: Pyflakes (unused imports, undefined names)
  - I: isort (import ordering)
  - C4: flake8-comprehensions
  - UP: pyupgrade (modern Python syntax)
  - PIE: flake8-pie (misc rules)
- Ignored rules:
  - E501: Line too long (handled by line-length setting)
  - F403, F405: Star imports allowed intentionally for convenience
- First-party module: `openrag` configured in isort settings

**Import Conventions:**
- Standard library imports at top
- Third-party imports grouped together (FastAPI, Ray, LangChain, etc.)
- Absolute imports from `openrag/` root only (not relative imports across packages)
- Pattern in code: `from components.ray_utils import`, `from utils.logger import`, `from routers.indexer import`
- Ray imports standard: `import ray`, `from ray.exceptions import RayTaskError, TaskCancelledError`
- Exception imports with wildcard allowed: `from utils.exceptions.vectordb import *`

**Import Organization:**
1. Python standard library: `import os`, `import asyncio`, `import json`, `import warnings`
2. Third-party libraries: `import ray`, `from fastapi import`, `from langchain_core.documents.base import Document`
3. Application imports: `from components...`, `from utils...`, `from routers...`, `from config import load_config`
4. Relative imports within same package only

**Path Aliases:**
- None detected as formally configured, but uses absolute imports from openrag root

## Error Handling

**Patterns:**
- All custom exceptions inherit from `OpenRAGError` (`openrag/utils/exceptions/base.py`)
- Specific exception hierarchy:
  - `EmbeddingError` for embedding failures
  - `VDBError` and subclasses for vector database errors (`VDBConnectionError`, `VDBInsertError`, `VDBSearchError`, `VDBPartitionNotFound`, `VDBFileNotFoundError`, etc.)
- Exception instantiation includes code and status_code:
  ```python
  raise VDBConnectionError(message="Connection failed", **kwargs)
  raise VDBInsertError(message="Insert failed", status_code=422, **kwargs)
  ```
- API error responses use `exception.to_dict()` which returns `{"detail": "[CODE]: message", "extra": {...}}`
- Ray-specific exceptions handled in `call_ray_actor_with_timeout()` in `openrag/components/ray_utils.py`:
  - `TimeoutError`: Task exceeded timeout
  - `asyncio.CancelledError`: Calling coroutine cancelled
  - `TaskCancelledError`: Ray task was cancelled
  - `RayTaskError`: Ray task failed
- Try-except blocks with specific exception handling, not bare except

**Example:**
```python
async def call_ray_actor_with_timeout(
    future: ray.ObjectRef,
    timeout: float,
    task_description: str = "Ray task",
) -> Any:
    try:
        result = await asyncio.wait_for(asyncio.gather(future), timeout=timeout)
        return result[0]
    except TimeoutError:
        logger.warning(f"{task_description} timed out, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise
    except RayTaskError as e:
        raise RuntimeError(f"{task_description} failed") from e
```

## Logging

**Framework:** Loguru (via `from utils.logger import get_logger`)

**Patterns:**
- Get logger instance: `logger = get_logger()`
- Logger function calls: `logger.info()`, `logger.warning()`, `logger.debug()`, `logger.error()`
- Context binding: `logger.bind(file_id=file_id, partition=partition).info("Message")`
- Structured logging with extra fields for context
- Log output formats:
  - Stdout: `{LEVEL} | {module}:{function}:{line} - {message} [extra_fields]`
  - File (JSON): serialized for Grafana/ELK ingestion
- Configuration in `utils/logger.py` with `escape_markup()` to prevent markup injection

**Example:**
```python
from utils.logger import get_logger
logger = get_logger()
logger.bind(file_id=file_id, partition=partition).info("Document processed")
```

## Comments

**When to Comment:**
- Complex algorithms requiring explanation
- Non-obvious business logic
- Bug workarounds with context
- Warning about performance implications

**JSDoc/TSDoc:**
- Module-level docstrings: Present in most files
- Function docstrings: Used for public API functions, class methods
- Format: triple-quoted strings with description, Args, Returns sections
- Example test file header:
  ```python
  """
  Unit tests for media_loader audio processing functionality.

  These tests validate the pydub operations used in media_loader.py without
  importing the full module (which has complex dependencies).
  """
  ```
- Loaders have docstring documentation with abstract methods marked with `@abstractmethod`

## Function Design

**Size:**
- Functions generally under 50 lines
- Async functions can be longer when handling orchestration
- Test methods typically 10-20 lines

**Parameters:**
- Type hints used throughout: `def func(param: str) -> dict:`
- Optional parameters: `partition: list[str] = None`
- Default values common: `timeout: int = 30`, `top_k: int = 5`
- Keyword-only parameters: Used in async signatures like `async def async_search(...)`

**Return Values:**
- Explicit return types: `-> list[Document]`, `-> dict`, `-> Any`
- Async functions return awaited types: `async def async_add_documents(...) -> None:`
- Dictionary returns for status/response objects
- List returns for collections

## Module Design

**Exports:**
- Explicit imports in `__init__.py` files
- Example: `openrag/components/__init__.py` may export key classes
- Ray actors exposed via naming convention: `ray.get_actor("Vectordb", namespace="openrag")`

**Barrel Files:**
- Used sparingly in exception modules
- Example: `from utils.exceptions.vectordb import *` (wildcard import allowed per ruff config)

---

*Convention analysis: 2026-02-10*
