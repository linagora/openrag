# OpenRAG Agent Guide

## Build, Lint, and Test Commands

### Dependencies
```bash
# Install dependencies (uv package manager)
uv sync

# Install dev dependencies
uv sync --group dev

# Install lint dependencies
uv sync --group lint
```

### Development Server
```bash
# GPU deployment
docker compose up -d

# CPU deployment
docker compose --profile cpu up -d

# Rebuild and run
docker compose up --build -d
```

### Testing
```bash
# Run all unit tests
uv run pytest

# Run a single test file
uv run pytest openrag/components/indexer/chunker/test_chunking.py

# Run tests matching a pattern
uv run pytest -k "test_chunk"

# Run with verbose output
uv run pytest -v

# Run integration tests (requires running server)
uv run pytest -m integration

# Run tests with coverage
uv run pytest --cov=openrag
```

### Linting and Formatting
```bash
# Check code style
uv run ruff check openrag/ tests/

# Auto-fix linting issues
uv run ruff check --fix openrag/ tests/

# Format code
uv run ruff format openrag/ tests/

# Check formatting without modifying
uv run ruff format --check openrag/ tests/
```

### CI/CD
```bash
# Run API integration tests locally with act
act -j api-tests -W .github/workflows/api_tests.yml --bind
```

## Code Style Guidelines

### Imports
- Use **absolute imports** from the `openrag/` directory (Python path root)
- Group imports: standard library → third-party → first-party (`openrag.*`)
- Use `from openrag.X import Y` not relative imports across packages
- Isort configuration: `known-first-party = ["openrag"]`

```python
# Correct
from components.ray_utils import call_ray_actor_with_timeout
from utils.logger import get_logger
from config import load_config

# Avoid
from ..ray_utils import ...  # Only use within same package
```

### Formatting
- **Line length**: 120 characters (configured in `pyproject.toml`)
- **Target Python**: 3.12+
- Use **double quotes** for strings
- Use **4 spaces** for indentation (no tabs)
- Follow Black-compatible formatting (Ruff format)

### Type Hints
- Use **type hints** for function parameters and return values
- Use `|` for union types (Python 3.10+ syntax)
- Use `Optional[T]` or `T | None` for optional values
- Use `list[T]`, `dict[str, Any]` for collections

```python
def process_file(file_id: str, partition: str | None = None) -> dict[str, Any]:
    """Process a file and return metadata."""
    ...
```

### Naming Conventions
- **Functions/variables**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_CASE`
- **Private members**: `_leading_underscore`
- **Ray Actors**: `PascalCase` (e.g., `Indexer`, `TaskStateManager`)
- **Test functions**: `test_<description>`

### Error Handling
- Use **custom exceptions** from `openrag/utils/exceptions/`
- All exceptions inherit from `OpenRAGError`
- Include `code`, `message`, and optional `status_code`
- Use specific exception types: `VDBError`, `EmbeddingError`

```python
from utils.exceptions import OpenRAGError, VDBError

# Raise error with code and message
raise VDBError(message="Failed to connect", code="VDB_001", status_code=503)

# Custom exception with extra context
raise OpenRAGError(
    message="File not found",
    code="FILE_NOT_FOUND",
    status_code=404,
    file_id=file_id
)
```

### Logging
- Use **Loguru** with structured logging via `get_logger()`
- Include contextual data using `.bind()`
- Never log secrets or sensitive data

```python
from utils.logger import get_logger

logger = get_logger()

# Log with context
logger.bind(file_id=file_id, partition=partition).info("Processing file")

# Error logging with exception
logger.bind(error=str(e)).error("Failed to process document")
```

### Async/Await
- Use `async def` for I/O operations (database, HTTP, Ray)
- Always `await` async calls
- Use `asyncio.gather()` for concurrent independent operations
- Use `call_ray_actor_with_timeout()` for Ray actor calls

```python
from components.ray_utils import call_ray_actor_with_timeout

# Concurrent operations
results = await asyncio.gather(
    task1(),
    task2(),
    task3()
)

# Ray actor with timeout
result = await call_ray_actor_with_timeout(
    future=indexer.process.remote(data),
    timeout=30,
    task_description="Processing document"
)
```

### Ray Actors
- Ray Actors are initialized in `openrag/api.py`
- Access actors via `ray.get_actor(name, namespace="openrag")`
- All actor methods called with `.remote()`

```python
import ray

# Get actor reference
vectordb = ray.get_actor("Vectordb", namespace="openrag")
indexer = ray.get_actor("Indexer", namespace="openrag")

# Call methods
await vectordb.async_search.remote(query=query, partition=partition)
```

### Configuration
- Configuration via **Hydra** with YAML files in `.hydra_config/`
- Access config via `load_config()` from `config.py`
- Environment variables override config values

```python
from config import load_config

config = load_config()
chunk_size = config.chunker.size
```

### API Patterns
- FastAPI routers in `openrag/routers/`
- Use dependency injection for shared resources
- Return `JSONResponse` for custom error responses
- Use Pydantic models for request/response validation

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter()

class DocumentRequest(BaseModel):
    text: str
    partition: str | None = None

@router.post("/documents")
async def create_document(req: DocumentRequest, user: User = Depends(get_current_user)):
    ...
```

### Testing Guidelines
- Unit tests: `openrag/components/**/test_*.py` (pytest)
- Integration tests: `tests/api_tests/*.py`
- Use pytest fixtures from `conftest.py`
- Mark tests: `@pytest.mark.integration` or `@pytest.mark.unit`

```python
import pytest

@pytest.mark.unit
def test_chunking():
    assert result == expected

@pytest.mark.integration
async def test_api_endpoint():
    response = await client.post("/v1/chat/completions", json={...})
    assert response.status_code == 200
```

### Documentation
- Docstrings: **Google style** or **reStructuredText**
- Include type hints in docstrings if not obvious
- Document complex algorithms and business logic

```python
def process_chunk(chunk: Chunk) -> Embedding:
    """Process a document chunk and generate embedding.

    Args:
        chunk: The chunk to process

    Returns:
        Generated embedding vector

    Raises:
        EmbeddingError: If embedding generation fails
    """
    ...
```

## Key Files and Directories

```
openrag/
├── api.py                  # FastAPI app entry point, Ray initialization
├── routers/                # API route handlers
├── components/             # Core components (Indexer, Vectordb, Pipeline)
│   ├── indexer/           # Document ingestion, chunking, embedding
│   ├── pipeline.py        # RAG pipeline orchestration
│   └── websearch/         # Web search integration
├── utils/                  # Shared utilities
│   ├── exceptions/        # Custom exception classes
│   ├── logger.py          # Logging configuration
│   └── config.py          # Configuration loading
├── models/                 # Pydantic models
└── prompts/                # LLM prompt templates
```

## Important Notes

- **Never commit secrets** - use `.env` files (not in repo)
- **Ray namespace** is always `"openrag"` for all actors
- **Milvus** is the vector database with hybrid search (dense + BM25)
- **Authentication** uses token-based auth with RBAC
- **Partition-based** multi-tenant document organization
- **OpenAI-compatible** API format for chat completions
