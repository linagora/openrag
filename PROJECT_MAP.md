# Project map

## [TECH_STACK]

- Python `>=3.12`; PyMilvus `3.0.1` (locked). Checked 2026-10-26; source: `pyproject.toml` and `uv.lock`.

## [SYSTEM_FLOW]

- Chunk metadata is prepared for Milvus's dynamic field. String values are shortened when needed to meet the 65,536-byte dynamic-field limit; unsupported values fail before the insert call with the vector store's insert error.

## [ARCHITECTURE]

- `MilvusVectorStore` owns chunk-to-entity conversion and writes through PyMilvus. Serialization validation errors surface as `VDBInsertError` before a write; the collection schema and public API are unchanged.

## [DECISIONS]

- Map PyMilvus serialization validation errors to `VDBInsertError` because preflight runs before the existing insert error handler; preserve the underlying exception as the cause.

## [OUT_OF_SCOPE]

- No schema, API, configuration, or dependency changes.

## [ORPHANS & PENDING]

- None.

## [VERIFICATION STATUS]

- Unsupported `Decimal` metadata is rejected as `VDBInsertError` before insertion: pass.
- Focused storage and pipeline suites: 247 passed.
- Full unit suite: 6,375 passed, 304 skipped; baseline was 6,374 passed, 304 skipped.
- Ruff check, Ruff format check, and `git diff --check`: pass.
- Final diff review: pass; only row-limit handling, regression coverage, and this map changed.
