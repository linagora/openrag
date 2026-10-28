# XLSX ingestion: behavior, limits, and validation

## Current design

OpenRAG reads .xlsx files with **OpenPyXL in read-only mode** after a bounded OOXML ZIP/XML preflight. The production parser has one reader path; Calamine and runtime backend/fallback settings are not part of this implementation.

Each visible worksheet is indexed independently. The parser preserves the workbook filename, worksheet name and index, physical Excel row numbers, header and data ranges, and exact row membership. It emits bounded Markdown table batches incrementally into the existing tabular indexing pipeline.

Hidden and veryHidden worksheets are skipped and counted. Hidden or filtered rows and hidden columns inside a visible worksheet remain indexable because visibility is presentation metadata and may otherwise hide searchable business data.

## Processing flow

1. Route an upload to the XLSX parser by its recognized extension or XLSX MIME type.
2. Validate the package, relationships, worksheet coordinates, shared strings, and resource limits before OpenPyXL opens the workbook.
3. Open the workbook with <code>read_only=True</code>, <code>data_only=True</code>, and <code>keep_links=False</code>.
4. Select a header independently for each visible nonempty sheet.
5. Retain nonempty rows above the header as searchable worksheet context.
6. Stream nonempty data rows into row-count and byte-bounded table batches.
7. Chunk, embed, and store one batch at a time through the shared CSV/XLSX tabular pipeline.

The compatibility <code>parse()</code> API collects every emitted block and should not be used for large-file ingestion. Production ingestion uses <code>iter_batches()</code>.

Formula-free workbooks retain the single-reader path. When the bounded preflight finds formulas on a visible sheet, the parser opens a second read-only OpenPyXL stream and pairs formula text with the cached values row by row. It never materializes the worksheet or a formula map in memory.

## Header selection

By default, a bounded scan examines at most the first 32 physical rows and one batch byte budget. It can skip one or more preamble rows when a later textual row:

- has at least two distinct labels;
- is wider than all preceding nonempty preamble rows (or follows repeated-value title rows) and follows a blank separator; and
- is followed by a row containing values in the candidate header columns.

Layouts without a blank boundary remain ambiguous and keep the first nonempty row, even if later rows are wider. Equal-width layouts and single-column tables also keep their first nonempty row. This conservative rule avoids silently consuming an ordinary first data record as a header.

For deterministic exports, set a global physical header row:

    loader:
      xlsx_header_row: 3

<code>XLSX_HEADER_ROW=3</code> provides the same global override. A selected row must exist and contain at least one non-whitespace value.

Per-sheet overrides use exact, case-sensitive worksheet names:

    loader:
      xlsx_header_row: 2
      xlsx_sheet_header_rows:
        People: 3
        Projects: 5
        Notes: null

A sheet override wins over the global row. <code>null</code> restores automatic detection for that sheet. Unknown, hidden, empty, or out-of-range targets fail before a reader emits data.

Nonempty preamble rows are appended to each table block as searchable worksheet context. Repeated values in one preamble row are displayed once, while distinct values and separate rows are retained. Context has the same byte and token limits as the table content.

## Values, formulas, and citations

Stored scalar values are indexed. Booleans, dates, datetimes, times, durations, Unicode text, zero, and <code>false</code> are preserved in stable text form. Blank header cells receive generated unique names, and duplicate explicit headers are disambiguated.

XLSX is an OOXML package, so CSV-style character-set detection does not apply. XML parts using UTF-8, with or without a byte-order mark, or correctly BOM-marked UTF-16 are supported, including either UTF-16 byte order. Unicode text is preserved without normalization, including composed and decomposed accents, right-to-left scripts, CJK text, emoji, and embedded byte-order-mark characters. XML with malformed bytes or a mismatched encoding declaration is rejected as <code>INVALID_XLSX</code> rather than decoded with replacement characters.

Formula cells index both the cached scalar result and the inert formula expression, for example <code>42 [formula: =SUM(B2:B10)]</code>. This makes calculated values, operators, ranges, and function names searchable without executing workbook code. OpenRAG does not recalculate workbooks. A formula without a cached value is still indexed as formula text, increments <code>sheet_missing_formula_values</code>, and logs a warning. Cached results may be stale.

Numbers use Python's stable scalar representation, preserving all precision returned by OpenPyXL instead of rounding to the worksheet's visual format. Unicode mathematical notation such as <code>∑</code>, <code>√</code>, <code>≤</code>, <code>±</code>, superscripts, and Greek letters is preserved verbatim. For an exact displayed percentage, currency, or rounded value, save that display text in a separate cell; Excel number-format rendering is not treated as the stored value.

Every batch includes:

- <code>sheet_name</code>, <code>sheet_index</code>, and <code>table_id</code>;
- <code>table_header_row</code>, <code>table_header_range</code>, and <code>table_columns</code>;
- <code>table_row_numbers</code>, <code>table_row_start</code>, and <code>table_row_end</code>;
- <code>cell_range</code> as the batch's bounding worksheet range;
- <code>sheet_context_row_numbers</code> when preamble context exists;
- <code>hidden_sheets_skipped</code> and the formula policy.
- <code>sheet_formula_cells</code> and <code>sheet_missing_formula_values</code> for formula auditability.

<code>cell_range</code> is a bounding range. Exact membership is represented by the row numbers and selected column metadata after chunk splitting. Transitional <code>csv_*</code> aliases keep the existing tabular chunking and sibling-row retrieval path compatible.

## Resource and safety limits

| Limit | Default |
| --- | ---: |
| Compressed archive | 256 MiB |
| ZIP entries | 20,000 |
| Total uncompressed archive | 1 GiB |
| Individual ZIP entry | 256 MiB |
| Compression ratio per entry | 200 |
| Total eagerly loaded metadata, XML, and chart media | 8 MiB |
| Shared-string XML | 32 MiB |
| Shared-string entries | 250,000 |
| Worksheets, including hidden sheets | 128 |
| Row coordinate | 1,048,576 |
| Column coordinate | 4,096 |
| Physical cells across worksheets | 5,000,000 |
| Worksheet rectangle, including gaps | 5,000,000 positions |
| Cell characters | 32,767 |
| Rendered Markdown batch | 2 MiB or 10,000 data rows |

Override limits in YAML:

    loader:
      xlsx_batch_size: 5000
      xlsx_limits:
        max_batch_bytes: 1048576
        max_shared_strings: 100000

Invalid packages raise <code>INVALID_XLSX</code> (422). Resource limits raise <code>XLSX_LIMIT_EXCEEDED</code> (413). A header, context, or data row that cannot fit in one byte-bounded batch fails explicitly.

The validation pass is an intentional extra sequential read. It validates actual cells instead of trusting worksheet dimension declarations, prevents sparse ranges from causing excessive traversal, rejects unsafe ZIP paths and relationships, and bounds shared metadata before OpenPyXL allocates workbook objects. Limits constrain accepted input; they are not an exact resident-memory ceiling.

## Deliberate scope boundaries

| Workbook case | Current behavior | Recommended follow-up |
| --- | --- | --- |
| Multiple separate tables in one worksheet | Treated as one table | Add explicit table-region segmentation in a separate feature |
| Multi-row or merged column headers | Not inferred | Use an explicit single header row or normalize the export |
| Headerless sheets | First nonempty row becomes the header | Add an explicit headerless mode only with a defined column schema |
| Formatted display values | Exact stored scalar value is indexed | Put presentation text in a separate cell when visual formatting carries meaning |
| Formula recalculation | Never performed | Recalculate in Excel/LibreOffice before upload |
| Images, charts, comments, shapes | Not indexed | Handle through dedicated document/media extraction |
| Password-protected or encrypted workbooks | Rejected | Decrypt before upload |
| .xls, .xlsb, and .ods | Unsupported by this route | Convert to .xlsx or implement separate parsers |
| Hard time or memory termination | Cooperative only | Use process isolation for strict worker enforcement |

These boundaries should remain explicit in the first PR. Multiple-table segmentation and merged multi-row header interpretation are ambiguous product features and should not be added as parser heuristics without fixtures and expected retrieval behavior.

## Validation and performance

Focused tests cover archive safety, relationships, coordinates, misleading dimensions, shared strings, formulas, error values, hidden sheets, hidden rows and columns, empty data, UTF-8 and UTF-16 OOXML, Unicode scripts and normalization forms, dates, duplicate headers, global and per-sheet headers, multiple preamble rows, ambiguous layouts, byte limits, early close, chunk splitting, citations, retrieval grouping, and indexing cleanup.

The local benchmark now measures the current OpenPyXL-only parser in a fresh process per run. It includes preflight and parsing and excludes fixture generation, embedding, storage, and chat:

    PYTHONPATH=openrag python tests/load/xlsx_ingestion/benchmark.py --output /tmp/xlsx-audit.json

[Benchmark source](https://github.com/linagora/openrag/blob/develop/tests/load/xlsx_ingestion/benchmark.py) · [Saved historical reader comparison](https://github.com/linagora/openrag/blob/develop/tests/load/xlsx_ingestion/audit-results.json)

The saved comparison explains the reader decision: Calamine parsed the synthetic fixtures faster, while OpenPyXL provided the required error-cell behavior, predictable workbook semantics, and lower peak memory on the large-row fixture. Calamine was therefore removed from the runtime dependency set rather than retained as an unused fallback.

## Remaining deployment validation

Before production rollout:

1. Run representative company exports through upload → indexing → Milvus → retrieval, including wide sheets and shared-string-heavy files.
2. Measure concurrent uploads against the actual worker/container memory limit.
3. Validate the deployed embedding tokenizer and answer quality for rows split into sibling chunks.
4. Exercise worker termination and retry behavior; cancellation waits for the active parsing thread to return.
5. Confirm that stored scalar values, inclusion of hidden rows/columns, and exclusion of hidden worksheets match the product policy.
