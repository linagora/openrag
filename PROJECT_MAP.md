# Project Map

[TECH_STACK]
- Python 3.12+ (`pyproject.toml`); unit tests run with pytest.

[SYSTEM_FLOW]
- CSV files are parsed incrementally and indexed one batch at a time.
- CSV batches skip contextualization; when topic tagging is enabled, language is detected once after all batches finish.
- The bounded topic-tag sample preserves chunk content so language detection can ignore generated English source headers.
- When the content budget is exhausted, later sample chunks leave content unset so language detection can fall back to display text.

[ARCHITECTURE]
- CSV batch orchestration: `openrag/services/workers/csv_pipeline.py`.
- CSV batch flow tests: `tests/unit/services/workers/test_csv_pipeline.py`.
- Bounded topic-tag sample: `openrag/core/indexing/topic_tags.py`.

[DECISIONS]
- Keep CSV language detection on the combined post-batch sample and preserve the source content used to filter generated headers.

[OUT_OF_SCOPE]
- CSV parsing, API contracts, and persistence schemas are unchanged.

[ORPHANS & PENDING]
- None.

[VERIFICATION STATUS]
- CSV language-path test: detection runs once after three batches and excludes generated source headers.
- Topic-tag sample tests: text and source content stay within budget; exhausted content falls back to display text.
- CSV worker suite: 26 passed.
- Topic-tag suite: 10 passed.
- Unit suite: 6515 passed, 304 skipped.
