# Project Map

[TECH_STACK]
- Python 3.12+ (`pyproject.toml`); unit tests run with pytest.

[SYSTEM_FLOW]
- CSV files are parsed incrementally; each batch is indexed using a copy of the file row.
- Batch-derived per-file state, including a language resolved during a batch, is copied back before the next batch starts.
- CSV batches skip contextualization; when topic tagging is enabled, language is detected once after all batches finish.

[ARCHITECTURE]
- CSV batch orchestration: `openrag/services/workers/csv_pipeline.py`.
- CSV batch flow tests: `tests/unit/services/workers/test_csv_pipeline.py`.

[DECISIONS]
- Preserve a language resolved in one batch for subsequent batches.
- In the current CSV flow, topic tagging resolves language once after all batches; the carry-over also covers any batch stage that resolves it.

[OUT_OF_SCOPE]
- CSV parsing, API contracts, and persistence schemas are unchanged.

[ORPHANS & PENDING]
- None.

[VERIFICATION STATUS]
- CSV worker baseline: 25 passed.
- Batch carry-over check: a language resolved in one batch is reused by later batches.
- Current CSV flow check: detector called once on the combined sample after three batches.
- CSV worker suite: 27 passed.
- Unit baseline: 6493 passed, 304 skipped; after: 6495 passed, 304 skipped.
