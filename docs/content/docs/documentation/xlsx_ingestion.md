# XLSX ingestion: behavior, limits, and audit

## Current scope

The first XLSX version reads visible worksheets, detects headers independently on each sheet, skipping obvious title rows, and indexes later nonempty rows as tables. It preserves worksheet names, physical row numbers, column positions, and source ranges. Hidden worksheets are skipped and counted. Separate tables inside one worksheet, merged-header interpretation, formulas recalculation, images, formatting, `.xls`, `.xlsb`, and `.ods` are not implemented.

The indexing worker consumes batches incrementally. The compatibility `parse()` API collects all blocks and should not be used for large-file ingestion. A row/byte limit controls batch size; it is not a hard process-memory limit.

## What the audit corrected

- **Data fidelity:** preserved leading empty columns in Calamine; aligned integral numbers and date rendering; routed Calamine workbooks containing Excel errors to OpenPyXL when that fallback is configured. With no fallback, unsupported cases fail explicitly. No error-cell data is silently discarded.
- **Headers:** generated names avoid collisions, including duplicate explicit names. Split chunks use escaped Markdown headers, so pipes and newlines cannot corrupt column boundaries.
- **Formula behavior:** only cached values are read. Missing cached values stay empty and produce a warning plus a per-sheet count (`sheet_missing_formula_values`); formulas are never executed. That count is repeated on each batch, so do not sum it across chunks. Cached results may be stale.
- **Dimensions:** inspected real cell coordinates before opening the reader. OpenPyXL ignores the declared worksheet dimension and uses validated bounds, preventing truncation from `A1:A1` and excessive padding from inflated dimensions.
- **Scalability:** added archive, worksheet, cell, shared-string, dense-range, text-expansion, and batch-byte limits. The validation pass is a deliberate extra read before indexing begins.
- **Chunking:** exercised the oversized question/answer and identity-cell failures seen in CSV. Both recursive and structured chunkers retain workbook metadata, produce bounded continuations, and keep sheets separate, including when consuming the collecting API.
- **Citations:** split chunks update row ranges and selected column positions. `cell_range` is a bounding range; exact membership is `table_row_numbers` × `table_column_indices`. `table_header_range` records the original header separately.
- **Failure handling:** readers close on early consumer exit. Fallback is allowed only for missing dependencies or recognized backend failures before the first emitted batch; timeout, memory, and programming failures are not retried. Late failures do not replay already indexed batches.
- **Pipeline regression checks:** XLSX uses the existing cancellation, per-attempt cleanup, replacement, and sheet-scoped sibling retrieval paths. These were tested with local fake embedding/storage ports; no external model endpoint was called.

## Reader choice

The default is **OpenPyXL without a fallback reader**. Every workbook therefore follows the same read-only OpenPyXL path after the shared OOXML validation pass. This configuration favors predictable behavior, Excel error-cell support, and lower peak memory on the large-row benchmark over Calamine's faster parsing times.

OpenPyXL uses read-only mode and validated worksheet bounds: [OpenPyXL optimized modes](https://openpyxl.readthedocs.io/en/stable/optimized.html). Shared strings and the validation pass still consume memory, so the general archive, sheet, cell, text, and batch limits remain necessary. Calamine remains available for explicit comparative testing, but the default configuration never selects it.

```yaml
loader:
  xlsx_batch_size: 10000
  xlsx_backend: openpyxl
  xlsx_fallback_backend: null
  xlsx_limits: {}
```

Existing `XLSX_BACKEND` or `XLSX_FALLBACK_BACKEND` environment overrides still take precedence. `XLSX_BATCH_SIZE` overrides the row limit; resource limits are overridden through `loader.xlsx_limits` in YAML or `XlsxLimits` in Python.

## Default resource limits

| Limit | Default |
| --- | --- |
| Compressed archive | 256 MiB |
| ZIP entries | 20,000 |
| Total uncompressed archive / individual entry | 1 GiB / 256 MiB |
| Compression ratio per entry | 200 |
| Metadata or styles XML | 8 MiB |
| Shared-string XML / string count | 32 MiB / 250,000 |
| Worksheets, including hidden sheets | 128 |
| Row coordinate / column coordinate | 1,048,576 / 4,096 |
| Physical cells across worksheets | 5,000,000 |
| Worksheet rectangle, including gaps | 5,000,000 positions |
| Calamine worksheet rectangle | 1,000,000 positions |
| Calamine expanded cell text per sheet | 64 MiB |
| Cell characters | 32,767 |
| Rendered Markdown batch | 2 MiB, or 10,000 data rows, whichever comes first |

Workbook/ZIP limits raise `XLSX_LIMIT_EXCEEDED` (413). Invalid packages raise `INVALID_XLSX` (422). A row or header that cannot fit within one byte-bounded batch is rejected explicitly. Calamine-specific limits select the configured fallback before any data is emitted, or raise a reader error if none is configured. Raising a limit requires a representative memory benchmark for the target worker; limits constrain inputs, not exact RSS.

Example byte-budget override:

```yaml
loader:
  xlsx_limits:
    max_batch_bytes: 1048576
    max_shared_strings: 100000
```

## Local validation results

**499 focused tests passed** across CSV/XLSX parsing, both chunkers, retrieval, parser configuration, worker cleanup, and storage adapters. Storage tests use fake ports; they do not substitute for a live Milvus run. Oversized-cell tests check bounded output, retained content, and source metadata with deterministic length counters; deployed tokenizer/embedding compatibility still needs integration testing.

The benchmark runs each reader in a fresh process, twice per fixture, and generates files in separate processes. Measurements include preflight and parsing, but exclude fixture generation, embedding, storage, and chat. The output row counts and SHA-256 digests match across readers and repeats. Peak RSS includes Python/library overhead and measures the highest resident memory observed by the OS.

Environment: macOS ARM64, Python 3.12.14, OpenPyXL 3.1.5, python-calamine 0.9.0. Times below are the mean of two runs; memory is the larger peak.

| Fixture | OpenPyXL time | OpenPyXL peak RSS | Calamine time | Calamine peak RSS |
| --- | --- | --- | --- | --- |
| 10,000 rows × 4 columns | 0.50 s | 55.1 MiB | 0.24 s | 54.0 MiB |
| 100,000 rows × 4 columns | 4.54 s | 65.4 MiB | 2.30 s | 98.2 MiB |
| 50,000 rows × 4 columns, shared strings | 2.36 s | 90.6 MiB | 1.23 s | 88.7 MiB |
| 200 rows × 256 columns | 0.53 s | 54.9 MiB | 0.26 s | 56.0 MiB |
| 1,000 rows × 4 columns, 2,048 characters/cell | 0.17 s | 58.7 MiB | 0.12 s | 59.8 MiB |

The ordinary fixtures use inline strings; the shared-string fixture contains 200,004 distinct entries, including headers. The 10× row increase raised OpenPyXL peak memory moderately and Calamine memory more substantially. Shared-string loading increased OpenPyXL memory; it was slightly higher than Calamine on that fixture. The deployed default uses only OpenPyXL for consistent workbook handling; the benchmark table is retained to make the performance tradeoff explicit.

Reproduce from the repository root with the project dependencies installed:

```sh
PYTHONPATH=openrag python tests/load/xlsx_ingestion/benchmark.py --output /tmp/xlsx-audit.json
```

[Benchmark source](../../../../tests/load/xlsx_ingestion/benchmark.py) · [Saved measurements](../../../../tests/load/xlsx_ingestion/audit-results.json)

## Remaining validation before production

1. Run representative real Excel exports through the deployed upload → indexing → Milvus → retrieval path, including wide sheets and shared-string-heavy files.
2. Check concurrent uploads against the worker/container memory limit; this audit measured one parser process at a time.
3. Validate the actual embedding tokenizer and context budget, plus answer quality for rows needing many sibling chunks. Retrieval expansion remains capped; a very large row is not guaranteed to be fully retrieved.
4. Test worker/process termination and retries. Cleanup is best effort, not transactional. Cancellation waits for the active parsing thread to return; hard time/memory enforcement would require process isolation.
5. Confirm intended interpretation of formatted numbers, merged cells, totals, and multiple tables. This version extracts stored scalar values, not Excel's visual presentation; midnight datetimes are rendered as dates for backend consistency.

The audit reduces known risks and adds reproducible coverage. It does not establish that every possible workbook is safe, that ingestion has no remaining scalability limits, or that deployment integration is already validated.


## Report titles and explicit header rows

By default, a bounded scan of the first 32 physical rows skips obvious single-value or repeated-value titles when followed by a multi-column textual header and aligned data. Ambiguous layouts retain the first nonempty row. The scan also stops at the batch byte budget. For exports with a title,
notes, or blank preamble rows, set `loader.xlsx_header_row: 3` in YAML or
`XLSX_HEADER_ROW=3` in the environment. The number is the physical Excel row,
starting at 1. The selected row must contain at least one value; an empty or
missing selected row fails explicitly instead of silently selecting another row.
Blank individual header cells still receive generated names.

This setting applies to every nonempty visible worksheet in uploads handled by
this deployment. Use it only for workbooks with a consistent header position.
Leave it unset for the existing first-nonempty-row behavior. Mixed sheet layouts can use the per-sheet overrides below.

Rows above the selected header are intentionally excluded from indexed content.
Merged titles above it are therefore supported without expanding merged cells.
Data rows keep their original physical coordinates, even across blank rows and
batch boundaries. The selection consumes the existing row iterator and does not
materialize a worksheet or change the archive, cell, or batch limits. Merged
headers, headerless sheets, and multiple tables are still outside this feature.


## Per-sheet header selection

Set exact, case-sensitive worksheet names in YAML. A sheet override wins over
`xlsx_header_row` (including its `XLSX_HEADER_ROW` environment override). A null
sheet override enables automatic detection even if a global row is configured.
Sheets absent from the mapping retain the global setting, or automatic selection
when that is unset.

```yaml
loader:
  xlsx_header_row: 2
  xlsx_sheet_header_rows:
    People: 3
    Projects: 5
    Notes: null
```

The Python equivalent is `XlsxParser(header_row=2,
sheet_header_rows={"People": 3, "Projects": 5, "Notes": None})`.
This mapping is deployment configuration, not a per-upload option; use it only
for uploads with the named sheets. Unknown names, hidden sheets, empty sheets,
and out-of-range selected rows fail explicitly. Name/visibility/bounds checks
run before any batch is emitted. An empty selected row is rejected when read;
a later sheet's failure still relies on the pipeline's existing cleanup.
Hidden sheets remain excluded. Row coordinates, batch limits, and streaming
behavior are preserved. Multiple tables and merged headers remain unsupported.

### Worksheet titles and preambles

Nonempty rows before a detected or explicit header are retained as worksheet
context, appended to every table chunk and included in embedding/search text.
`sheet_context_row_numbers` records their physical rows separately from data
row citations. Repeated values within a preamble row are displayed once (for
merged-title exports); distinct values and separate rows are retained. Blank
rows add no context. Context is subject to batch byte and chunk token budgets;
excessive context fails explicitly rather than being silently truncated.
