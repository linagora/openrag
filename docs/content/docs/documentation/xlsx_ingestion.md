# XLSX ingestion

OpenRAG indexes `.xlsx` workbooks as structured tabular content. It uses OpenPyXL in read-only mode and sends each visible worksheet through the same bounded indexing pipeline used for CSV data.

The parser is designed for retrieval, not spreadsheet execution: it preserves stored values, formulas, worksheet context, and physical row references, but it does not recalculate formulas or reproduce Excel's visual layout.

## What is indexed

For every visible, nonempty worksheet, OpenRAG preserves:

- workbook and worksheet names;
- the selected column header row and column names;
- physical Excel row numbers and cell ranges;
- nonempty rows above the header as worksheet context;
- stored scalar values, including numbers, booleans, dates, durations, and Unicode text;
- formula text and the cached value saved in the workbook, when available.

Rows are emitted incrementally as bounded Markdown table batches. Chunk metadata keeps rows scoped to their worksheet so sibling-row retrieval does not mix tables from different sheets.

Hidden and `veryHidden` worksheets are skipped. Hidden rows and columns inside a visible worksheet remain indexable because their visibility is presentation metadata rather than an access-control boundary.

## Header selection

Header selection runs independently for each worksheet.

By default, OpenRAG performs a bounded scan of the first 32 physical rows. It may select a later textual row when a blank row clearly separates a title or preamble from a wider table. Ambiguous layouts keep the first nonempty row to avoid silently discarding a data record.

For deterministic exports, configure a physical Excel row number:

```yaml
loader:
  xlsx_header_row: 3
```

The equivalent environment variable is `XLSX_HEADER_ROW=3`.

Per-sheet overrides use exact, case-sensitive worksheet names:

```yaml
loader:
  xlsx_header_row: 2
  xlsx_sheet_header_rows:
    People: 3
    Projects: 5
    Notes: null
```

A per-sheet value overrides the global row. `null` restores automatic selection for that sheet. An override that targets an absent, hidden, empty, or out-of-range worksheet fails before any content is indexed.

Blank header cells receive generated names such as `column_2`. Duplicate rendered names are made unique.

## Formulas and numerical values

Formula cells are indexed as a cached scalar paired with inert formula text, for example:

```text
42 [formula: =SUM(B2:B10)]
```

OpenRAG never executes workbook formulas or macros. When a formula has no cached value, its expression remains searchable and a warning is logged. Cached results may be absent or stale if the workbook was not recalculated and saved by Excel, LibreOffice, or another calculation engine after its inputs changed.

Numeric values use the scalar representation returned by OpenPyXL. Excel display formatting such as currency symbols, percentage formatting, and visual rounding is not treated as stored data. If the displayed form is semantically important, store it explicitly in another cell.

## Configuration

```yaml
loader:
  xlsx_batch_size: 10000
  xlsx_header_row: null
  xlsx_sheet_header_rows: {}
  xlsx_limits:
    max_batch_bytes: 2097152
    max_shared_strings: 250000
```

`xlsx_batch_size` limits the number of data rows emitted in one parser batch. `xlsx_limits` accepts the positive-integer limit names listed below. Values not specified retain their defaults.

OpenRAG detects independent table regions from content-bearing cells before extraction. Blank row and column separators define table boundaries, so tables may be stacked vertically, placed side by side, or start away from `A1`. Each detected table is extracted and chunked independently with its own stable table ID and exact worksheet range. Obvious title rows immediately above a table remain attached as searchable worksheet context.

## Default safety limits

Before OpenPyXL opens a workbook, OpenRAG validates the OOXML package, relationships, worksheet coordinates, shared strings, and resource bounds.

| Limit setting | Meaning | Default |
| --- | --- | ---: |
| `max_archive_bytes` | Compressed archive | 256 MiB |
| `max_entries` | ZIP entries | 20,000 |
| `max_uncompressed_bytes` | Total uncompressed archive | 1 GiB |
| `max_entry_bytes` | Individual ZIP entry | 256 MiB |
| `max_compression_ratio` | Compression ratio per entry | 200 |
| `max_metadata_bytes` | Total eagerly loaded XML, metadata, and chart media | 8 MiB |
| `max_shared_strings_bytes` | Shared-string XML | 32 MiB |
| `max_shared_strings` | Shared-string entries | 250,000 |
| `max_sheets` | Worksheets, including hidden sheets | 128 |
| `max_rows` | Row coordinate | 1,048,576 |
| `max_columns` | Column coordinate | 4,096 |
| `max_cells` | Physical cells across worksheets | 5,000,000 |
| `max_sheet_area` | Worksheet rectangle, including gaps | 5,000,000 positions |
| `max_tables` | Detected table regions across the workbook | 128 |
| `max_cell_chars` | Characters in one cell | 32,767 |
| `max_batch_bytes` | Rendered batch size | 2 MiB |

An upload whose bytes do not match its `.xlsx` extension is rejected with HTTP status 415 before indexing starts. A structurally invalid workbook that reaches the parser fails with `INVALID_XLSX` and status 422. Resource-limit failures use `XLSX_LIMIT_EXCEEDED` and status 413. Failed ingestion is cleaned up so partially indexed content is not left searchable.

The validation scan is intentional. It checks actual cells rather than trusting worksheet dimension declarations and prevents sparse coordinates, compressed archives, large shared-string tables, or eager metadata from causing uncontrolled parser work. These limits constrain accepted input; they are not an exact process-memory ceiling.

## Supported OOXML text encodings

XLSX stores text in XML, so CSV character-set detection does not apply. OpenRAG supports valid UTF-8 XML, with or without a byte-order mark, and correctly declared UTF-16 XML in either byte order. Malformed bytes or contradictory XML encoding declarations are rejected rather than decoded with replacement characters.

## Known limitations

| Workbook case | Current behavior | Recommended action |
| --- | --- | --- |
| Tables without a blank row or column boundary | Contiguous content is treated as one table | Add a fully blank separator row or column between independent tables |
| Merged or multi-row headers | Labels and pre-header context are retained, but merged spans and hierarchy are not reconstructed | Provide one explicit header row |
| Headerless worksheet | First nonempty row is retained and content remains searchable, but column semantics are not source-backed | Add a header row or configure a normalized export |
| Formula recalculation | Not performed | Recalculate and save the workbook before upload |
| Charts, images, comments, and shapes | Not indexed as worksheet data | Extract them through a dedicated document or media workflow |
| Password-protected or encrypted workbook | Rejected | Decrypt it before upload |
| `.xls`, `.xlsb`, `.ods`, and macro-enabled workbooks | Unsupported by the XLSX route | Convert to a plain `.xlsx` workbook or use a dedicated parser |
| Exact aggregation across very large tables or several workbooks | Semantic retrieval may not return every row needed for a calculation | Use an analytical query pipeline for exact full-dataset computation |

## Operational guidance

- Separate independent tables with at least one fully blank row or column and include explicit column headers.
- Recalculate and save formula-heavy workbooks before uploading them.
- Keep identifiers and units in explicit cells so they remain retrievable with numerical values.
- Use a fresh partition when comparing ingestion behavior across parser versions.
- Treat hidden rows and columns as indexable content; remove sensitive data from the workbook rather than relying on Excel visibility settings.
