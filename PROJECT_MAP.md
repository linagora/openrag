# Project map

## [TECH_STACK]

- Python `>=3.12`; PyMilvus `3.0.1` (locked). Checked 2026-10-26; source: `pyproject.toml` and `uv.lock`.

## [SYSTEM_FLOW]

- Plain Markdown table captions have repeated cell spacing collapsed. Captions containing code or indented text pass through unchanged; chunk metadata is then prepared for Milvus's dynamic field, shortening strings when needed to meet its 65,536-byte limit.

## [ARCHITECTURE]

- `caption_stage` normalizes table padding before chunking. `MilvusVectorStore` owns chunk-to-entity conversion and writes through PyMilvus; serialization validation errors surface as `VDBInsertError` before a write. The collection schema and public API are unchanged.

## [DECISIONS]

- Map PyMilvus serialization validation errors to `VDBInsertError` because preflight runs before the existing insert error handler; preserve the underlying exception as the cause.
- Skip caption normalization whenever code or indented text is present so transcription does not alter preformatted content.

## [OUT_OF_SCOPE]

- No schema, API, configuration, or dependency changes.

## [ORPHANS & PENDING]

- None.

## [VERIFICATION STATUS]

- Unsupported `Decimal` metadata is rejected as `VDBInsertError` before insertion: pass.
- Plain table padding is squeezed; captions with fenced, inline, or indented code are preserved: pass.
- Focused storage and pipeline suites: 248 passed.
- Full unit suite on the latest PR head: 6,376 passed, 304 skipped, 51 warnings.
- Ruff check, Ruff format check, and `git diff --check`: pass.
- Final diff review: pass; the follow-up covers the current inline findings and serializer error mapping.
