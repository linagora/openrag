# XLSX ingestion

OpenRAG indexes `.xlsx` and `.xlsm` workbooks as structured tabular content. It uses OpenPyXL in read-only mode and sends every detected table in each visible worksheet through the same bounded indexing pipeline used for CSV data.

The parser is designed for retrieval, not spreadsheet execution: it preserves stored values, formulas, worksheet context, and physical row references, but it does not recalculate formulas or reproduce Excel's visual layout.

## What is indexed

For every visible, nonempty worksheet, OpenRAG preserves:

- workbook and worksheet names;
- the detected table index, table count, boundaries, and stable table ID;
- the selected column header row and column names;
- flattened, source-backed merged column-header paths;
- expanded values for vertically merged row-header cells;
- physical Excel row numbers and cell ranges;
- nonempty rows above the header as worksheet context;
- stored scalar values, including numbers, booleans, dates, durations, and Unicode text;
- formula text and the cached value saved in the workbook, when available.

Rows are emitted incrementally as bounded Markdown table batches. Chunk metadata keeps rows scoped by both worksheet and table ID, so sibling-row retrieval does not mix independent tables from the same or different sheets.

Retrieval limits row-sibling expansion to `related_limit` chunks; setting it to zero disables expansion. A row split into more chunks than the limit can therefore be incomplete, and the cap is logged.

Hidden and `veryHidden` worksheets are skipped. Hidden rows and columns inside a visible worksheet are also excluded. Excel may mark filtered or collapsed rows as hidden, so those rows are also excluded. When rows are hidden, the worksheet context included in extracted chunks reports the count and warns that filtered or collapsed rows may be missing. Counts are also recorded in chunk metadata, and removing hidden columns does not change physical worksheet citations.

## Table detection

OpenRAG detects independent table regions from content-bearing cells during its bounded OOXML preflight. A fully blank row separates ordinary cell ranges, as do two or more fully blank columns. A single blank column between two blocks that cover exactly the same rows is treated as a spacer inside one table, because splitting there would cut every record in two (`Alice | 30` on one side, `Paris | 3000` on the other). The spacer is not indexed as a column; `table_worksheet_column_indices` keeps the real worksheet columns for citations. Side-by-side tables with the same rows and a single blank column between them are therefore read as one table: define them as Excel Tables, or leave two blank columns, to keep them apart. A single later row with the same column span as a preceding table is retained as a sparse continuation rather than being lost as a header-only table. Likewise, a block after a blank spacer row that has the same column span as the preceding table and whose first row contains a number, date, boolean, or error value continues that table under its header, since a header row is text. A block whose first row is all text may be the header of an independent table and stays separate; its header detection is reported as `ambiguous` when that cannot be decided. Source-defined Excel Tables are also treated as authoritative regions, so two Excel Tables remain independent even when their populated areas touch horizontally or vertically. Tables may therefore be stacked, placed side by side, or start away from `A1`, such as at `L30`.

A detected region with no data row below its first nonempty row is not a table: footnotes such as `Source: ...`, isolated totals such as `Total | 300`, and form-like label/value cells. Instead of treating that row as a header and emitting nothing, OpenRAG indexes these cells as text, grouped per worksheet:

```text
Worksheet cells outside tables:
- A1: Contrat 2026-114
- A3:B3: Client | ACME
- D5:E5: Montant | 12000

Worksheet: Contrat
```

These blocks carry `xlsx_content_kind: cells` (table blocks carry `table`), `sheet_name`, `sheet_index`, the sheet-qualified `cell_ranges` of each line, and `cell_row_numbers`. They are chunked as prose and have no `table_columns`. A workbook made only of such cells, such as a form sheet, is therefore indexed rather than rejected.

Every detected table is extracted and chunked independently. Its metadata includes:

- `table_id`, unique within the workbook;
- `table_index` and `worksheet_table_count`;
- `table_range` for the header and data rectangle;
- `table_region_range` when preceding title/context rows are also attached;
- `table_start_row`, `table_end_row`, `table_start_column`, and `table_end_column`;
- `sheet_name` and `sheet_index`, preserved on every batch and chunk;
- exact `table_header_range`, data-row numbers, and chunk `cell_range` citations.

An obvious merged title row immediately above a table is retained as searchable worksheet context rather than used as the column header. Context remains attached only to its originating table and worksheet. Worksheet context is repeated on every chunk of its table while it stays small. A context larger than a quarter of the chunk budget (and longer than the short pointer that replaces it) would crowd out the rows, so when the table needs splitting it is indexed once, with the table's first batch, as its own text chunks (`xlsx_content_kind: sheet_context`). The table chunks then keep the worksheet name and a line such as `Worksheet context (rows 1-3) is indexed in separate chunks.`, drop the context text from their metadata, and set `sheet_context_indexed_separately: true`. Table batches report `table_batch_index`, their one-based position within the table. Vertically disjoint tables are read in one forward pass per worksheet reader; overlapping table rows still require separate scans. The default safety limit permits at most 256 detected tables per visible worksheet; hidden worksheets are skipped and do not count.

## Merged and hierarchical headers

OpenRAG reconstructs hierarchy only when it is explicitly backed by merged-cell ranges in the workbook.

For column headers, consecutive horizontal merged rows above the leaf header are flattened into unambiguous Markdown column names. For example:

| Physical header levels | Indexed column name |
| --- | --- |
| `Person` → `Name` | `Person / Name` |
| `Directory` → `Contact` → `Email` | `Directory / Contact / Email` |

The original hierarchy remains available in `table_header_hierarchy`, and `table_header_hierarchy_rows` records its physical Excel rows. A lone merged cell spanning the complete table is treated as a title and retained as worksheet context. When a full-width parent is followed by smaller merged groups, however, it is retained as the top level of the hierarchy.

For row headers, a vertically merged single-column value is repeated across the data rows covered by that merge. Nested merges therefore preserve paths such as `Europe / France` for every applicable row. The relevant columns and per-row paths are stored in `table_row_header_columns` and `table_row_header_paths`. Chunking keeps only the paths for the physical rows present in that chunk.

Header reconstruction, value expansion, and metadata are scoped to the detected `table_id` and `sheet_name`. They cannot leak into a neighboring table or another worksheet, even when a workbook contains many independently detected tables.

## Header selection

Header selection runs independently for each detected table.

By default, OpenRAG performs a bounded scan of the first 32 physical rows in each detected region. It may select a later textual row when a blank row clearly separates a title or preamble from a wider table and data in the next three nonempty rows sits under most of that row's columns, so one record with an empty cell does not disqualify its header. A merged title spanning the table also selects the following row as the header. An Excel Table's header row is authoritative. An Excel Table saved without a header row (`headerRowCount="0"`) is labelled with its declared column names and every row is indexed as data; the same names are used when its header row is hidden or blank. A structural header row that turns out blank within the table's columns, or hidden, falls back to automatic detection instead of rejecting the workbook. Other ambiguous layouts keep the first nonempty row to avoid silently discarding a data record.

Every emitted table block reports how its header was selected. `table_header_source` distinguishes `automatic` and `structural_hint` (an Excel Table or merged header). The structured `table_header_detection` metadata contains the status (`confident` or `ambiguous`), selected row, bounded candidate-row list, machine-readable reason, and explanatory note. `table_header_detection_status` duplicates the status for simple filtering. Ambiguous automatic detection also emits one warning per table, while keeping the conservative first-row behavior unchanged.

An automatic result is marked confident only when the bounded scan finds strong structural evidence, such as a wider textual header after a blank separator, or unique textual labels followed by typed data. Equal-width all-text layouts remain ambiguous because values alone cannot prove which row is the header. A header that does not reach the outermost columns holding data is also reported as `ambiguous` (reason `header_narrower_than_data`), because those columns receive generated names. For deterministic ingestion, define the range as an Excel Table, whose header row is authoritative, or put the column headers in the first row of the range.

Header rows cannot be configured. Deployment-wide overrides were checked against every uploaded workbook, so a setting meant for one file rejected the others; a per-file or per-partition route may be added later.

When a single column header cannot fit within the configured chunk size, the chunker preserves it using an expanded effective budget recorded in `csv_effective_chunk_size`. The final continuation chunk also restores the reserved separator and worksheet/formula/display context allowance when checking that expanded budget. Worksheet context and the separator must still leave a positive table budget; otherwise indexing fails with a clear chunk-budget error.

Blank header cells receive generated names such as `column_2`. Duplicate rendered names are made unique.

## Formulas and numerical values

Formula cells are indexed as a cached scalar paired with inert formula text, for example:

```text
42 [formula: =SUM(B2:B10)]
```

OpenRAG never executes workbook formulas or macros. When a formula has no cached value, its expression remains searchable and a warning is logged. A formula whose cached result is an empty string, such as `=IF(A2="","",A2*2)` filled down a template, is indexed as an empty cell: it does not create rows, does not count as a formula, and is not reported as missing a cached value. Cached results may be absent or stale if the workbook was not recalculated and saved by Excel, LibreOffice, or another calculation engine after its inputs changed.

The bounded OOXML preflight detects formulas before OpenPyXL opens the workbook. OpenRAG normally uses one read-only, `data_only=True` reader. It opens the second read-only formula reader only when at least one formula exists in a visible row and column of a visible worksheet. Formulas located exclusively in skipped worksheets, hidden rows, or hidden columns therefore do not add the cost of a second workbook reader.

### Repeated formula patterns

Repeated formulas in a calculated column are normalized relative to their target cells. For example, `C2 = A2+B2`, `C3 = A3+B3`, and subsequent filled-down formulas share the R1C1 pattern `=RC[-2]+RC[-1]`.

Every contiguous run of at least two cells sharing the same normalized formula is generalized consistently. OpenRAG keeps the cached values in the Markdown cells and emits the formula once in chunk context, even for short runs. Physical columns are resolved to table headers so retrieval receives a semantic expression such as `total = quantity+unit_price`. Metadata retains the target range, normalized pattern, source columns, and physical rows:

```json
{
  "formula_groups": [
    {
      "target_column": "C",
      "target_header": "total",
      "range": "C2:C1000",
      "pattern": "=RC[-2]+RC[-1]",
      "semantic_expression": "total = quantity+unit_price",
      "source_columns": [
        {"column": "A", "header": "quantity"},
        {"column": "B", "header": "unit_price"}
      ],
      "start_row": 2,
      "end_row": 1000,
      "formula_count": 999
    }
  ]
}
```

Formula groups are scoped to the detected table and parser batch. During Markdown chunking, a group is retained only on chunks containing rows and columns intersecting its target range. A different formula interrupts the group: that exceptional cell keeps its complete expression inline and is described individually in `formula_cells` with its target coordinate, normalized pattern, semantic expression, source columns, and cache availability. At most 16 such cells are described per batch; the rest are summarized by a marker (`truncated: true`, `omitted_count`) whose `omitted_cells_by_column` lists, for each target column, the row ranges of its omitted cells, so a chunk receives the marker only when it holds an omitted cell's row and column together. The marker holds at most 64 row ranges over 32 named columns; beyond that the closest ranges are joined and further columns are pooled under `*`, and `omitted_ranges_coalesced: true` signals that it may then cover rows without an omitted formula.

The parser still enforces the configured batch byte limit. In the unusual case where adding generalized context would make an otherwise valid batch exceed that limit, formulas in that batch remain attached to their individual cells instead of failing workbook ingestion.

Absolute references remain absolute in the normalized pattern. Same-table A1 references are mapped to headers when every referenced column is visible and belongs to the table. A partially mapped range is kept intact and marked unresolved rather than described as a smaller range. Cross-sheet references, structured table references, named ranges, and dynamic references that cannot be resolved safely are preserved verbatim in `unresolved_references`; OpenRAG does not guess their meaning or execute them.

### Safe number and date display metadata

Numeric cells keep the raw scalar returned by OpenPyXL, so formatting never changes the value used for retrieval or calculation. OpenRAG also preserves a bounded, non-executable subset of display semantics for recognizable percentages, currencies, grouped or rounded numbers, scientific notation, dates, times, and durations. Repeated formats are grouped by physical column and parser batch rather than copied into every cell.

For example, a raw value of `0.08125` with the Excel format `0.00%` remains `0.08125` in the Markdown table. Its metadata and chunk context describe the percentage display and provide the deterministic example `0.08125 -> 8.13%`. Metadata records the physical column, table header, affected row ranges, safe format code, display kind, decimal precision, grouping flag, raw example, and display example:

```json
{
  "number_format_groups": [
    {
      "target_column": "D",
      "target_header": "discount_rate",
      "range": "D2:D1000",
      "kind": "percentage",
      "format_code": "0.00%",
      "decimal_places": 2,
      "uses_grouping": false,
      "example_raw": "0.08125",
      "display_example": "8.13%",
      "start_row": 2,
      "end_row": 1000,
      "cell_count": 999
    }
  ]
}
```

Only percentage and currency displays are stated in chunk text; dates are already readable in the cells and plain or scientific number formats only restate the raw value, so those groups are kept in `number_format_groups` metadata only. Formula and display context is optional text, so chunking keeps rows whole: it reserves at most a fifth of each chunk's token budget for that context, splits the rows once, and then adds formula lines first, then percentage and currency lines, while they fit. Lines that do not fit stay in metadata, and the chunk sets `supplemental_context_truncated: true`. Wide formatted tables therefore produce about as many chunks as unformatted ones instead of being split by column or rejected. During Markdown chunking, display groups are retained only when their physical rows and columns intersect the chunk. Context always states that raw values remain indexed. Complex conditional or colored formats, quoted custom units, text substitution, and format codes over the safety bound are retained only as workbook styling and are not interpreted or exposed as trusted display meaning. For locale-qualified currency formats, only an allowlisted currency symbol is retained; the locale directive itself is discarded and is never treated as instructions or semantic content. A group holds at most 32 disjoint row ranges; a format applied to more separate runs of rows is split into several groups, so each group's rows stay exact. At most 64 groups are described in one parser batch; truncation is explicit in metadata (`number_format_groups_truncated`). Supplemental human-readable display context is omitted before it can make an otherwise valid batch exceed the byte limit.

## Configuration

```yaml
loader:
  xlsx_batch_size: 10000
  xlsx_limits:
    max_batch_bytes: 2097152
    max_shared_strings: 250000
    max_tables: 256
```

`xlsx_batch_size` limits the number of data rows emitted in one parser batch and can also be set with `XLSX_BATCH_SIZE` (see the [environment reference](/openrag/documentation/env_vars/#excel-workbook-ingestion)). A batch may flush earlier to stay within `max_batch_bytes`. Parser batches are then split into retrieval chunks according to the configured chunk token budget; batch size is not chunk size.

`xlsx_limits` accepts the limit names listed below through YAML configuration. Batch size and limit values must be positive integers; booleans, fractional values, and unknown limit names are rejected. Values not specified retain their defaults. Increase limits only after measuring resource use for the intended workload; they do not impose a hard memory ceiling across simultaneous indexing jobs.

## Default safety limits

Before OpenPyXL opens a workbook, OpenRAG validates the OOXML package, relationships, worksheet coordinates, shared strings, and resource bounds.

| Limit setting | Meaning | Default |
| --- | --- | ---: |
| `max_archive_bytes` | Compressed archive | 256 MiB |
| `max_entries` | ZIP entries | 20,000 |
| `max_uncompressed_bytes` | Total uncompressed archive | 1 GiB |
| `max_entry_bytes` | Individual ZIP entry | 256 MiB |
| `max_compression_ratio` | Compression ratio per entry | 200 |
| `max_metadata_bytes` | Total size of the parts the reader loads whole: workbook, styles, theme, document properties, worksheet relationships, and chart sheets with their drawings, charts, and images. Pivot caches, comments, and worksheet drawings or media are never loaded and only count toward the archive limits | 8 MiB |
| `max_shared_strings_bytes` | Shared-string XML | 32 MiB |
| `max_shared_strings` | Shared-string entries | 250,000 |
| `max_sheets` | Worksheets, including hidden sheets | 128 |
| `max_rows` | Row coordinate of a cell with content | 1,048,576 |
| `max_columns` | Column coordinate of a cell with content | 4,096 |
| `max_cells` | Physical cells across worksheets, empty styled cells included | 5,000,000 |
| `max_sheet_area` | Traversal rectangle from `A1` to the last content-bearing cell or a declared Excel Table's bottom-right corner, including leading space and gaps | 5,000,000 positions |
| `max_tables` | Detected table regions per visible worksheet, and Excel Tables per worksheet | 256 |
| `max_merged_ranges` | Merged-cell ranges across the workbook | 100,000 |
| `max_cell_chars` | Characters in one cell | 32,767 |
| `max_batch_bytes` | Rendered batch size | 2 MiB |

Empty cells that only carry formatting, such as a stray styled cell at `XFD1` or `E1048576`, do not count toward the coordinate or area limits; merged ranges are clipped to them. Coordinates outside the Excel grid are rejected as invalid.

An upload missing the OOXML parts required by its `.xlsx` or `.xlsm` extension is rejected with HTTP status 415 before indexing starts. Macro-enabled content uploaded with an `.xlsx` name, or another structurally invalid workbook that reaches the parser, fails with `INVALID_XLSX` and status 422; use `.xlsm` for a macro-enabled workbook. Resource-limit failures use `XLSX_LIMIT_EXCEEDED` and status 413. Failed ingestion triggers best-effort cleanup; if cleanup fails during a re-index of an existing file, partial content can remain searchable until it is removed successfully.

The validation scan is intentional. It checks actual cells rather than trusting worksheet dimension declarations and prevents sparse coordinates, compressed archives, large shared-string tables, or eager metadata from causing uncontrolled parser work. These limits constrain accepted input; they are not an exact process-memory ceiling.

## Supported OOXML text encodings

XLSX stores text in XML, so CSV character-set detection does not apply. OpenRAG supports valid UTF-8 XML, with or without a byte-order mark, and correctly declared UTF-16 XML in either byte order. Malformed bytes or contradictory XML encoding declarations are rejected rather than decoded with replacement characters.

## Known limitations

| Workbook case | Current behavior | Recommended action |
| --- | --- | --- |
| Touching ranges without explicit Excel Table definitions | Their shared populated area is inherently ambiguous and is treated as one table | Define each range as an Excel Table, or add a fully blank separator row or two blank columns. Touching Excel Tables are extracted independently |
| Merged title above a single header row | The title is retained as table context and the following row becomes the header | No action is required when the title spans the detected table |
| Merged column-header groups | Consecutive source-backed levels are flattened and retained as hierarchy metadata; arbitrary visual layouts without merged ranges remain ambiguous | Use actual merged ranges or a single explicit header row |
| Merged row headers | Vertically merged single-column values are expanded through their covered data rows and recorded as per-row hierarchy metadata | No action is required for ordinary vertical row-header merges |
| Headerless worksheet | The first nonempty row is conservatively treated as the header | Add a header row, or define the range as an Excel Table |
| Formula recalculation | Not performed | Recalculate and save the workbook before upload |
| Charts, images, comments, and shapes | Not indexed as worksheet data | Extract them through a dedicated document or media workflow |
| Password-protected or encrypted workbook | Rejected at upload with HTTP 422 `ENCRYPTED_OFFICE_FILE` ("This file is password-protected or encrypted"); encrypted `.docx` and `.pptx` uploads get the same response | Remove the password and upload it again |
| `.xlsm` macro-enabled workbook | Tabular cells, cached values, and formula text are indexed; VBA is neither retained nor executed | Upload it directly when the workbook's tabular content is needed |
| `.xls`, `.xlsb`, or `.ods` | Unsupported by the XLSX route | Convert to a plain `.xlsx`/`.xlsm` workbook or use a dedicated parser |
| Exact aggregation across very large tables or several workbooks | Semantic retrieval may not return every row needed for a calculation | Use an analytical query pipeline for exact full-dataset computation |

## Operational guidance

- Define independent ranges as Excel Tables when they touch. Otherwise, separate them with at least one fully blank row, or two fully blank columns. Add explicit headers.
- Recalculate and save formula-heavy workbooks before uploading them.
- Macros are never executed during ingestion. `.xlsm` support reads only its OOXML worksheet data with VBA retention disabled.
- Keep identifiers and units in explicit cells so they remain retrievable with numerical values.
- Use a fresh partition when comparing ingestion behavior across parser versions.
- Hidden rows and columns are excluded, but workbook visibility should still not be treated as a general authorization mechanism outside OpenRAG ingestion.
