# XLSX ingestion

OpenRAG indexes `.xlsx` workbooks as structured tabular content. It uses OpenPyXL in read-only mode and sends every detected table in each visible worksheet through the same bounded indexing pipeline used for CSV data.

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

Hidden and `veryHidden` worksheets are skipped. Hidden rows and columns inside a visible worksheet are also excluded. Their counts are recorded in chunk metadata, and removing hidden columns does not change physical worksheet citations.

## Table detection

OpenRAG detects independent table regions from content-bearing cells during its bounded OOXML preflight. A fully blank row or column separates tables, so tables may be stacked vertically, placed side by side, or start away from `A1`, such as at `L30`.

Every detected table is extracted and chunked independently. Its metadata includes:

- `table_id`, unique within the workbook;
- `table_index` and `worksheet_table_count`;
- `table_range` for the header and data rectangle;
- `table_region_range` when preceding title/context rows are also attached;
- `table_start_row`, `table_end_row`, `table_start_column`, and `table_end_column`;
- `sheet_name` and `sheet_index`, preserved on every batch and chunk;
- exact `table_header_range`, data-row numbers, and chunk `cell_range` citations.

An obvious merged title row immediately above a table is retained as searchable worksheet context rather than used as the column header. Context remains attached only to its originating table and worksheet. Detection and chunking are covered for at least ten tables in one worksheet and for multiple tables across multiple worksheets. The default safety limit permits at most 128 detected tables across a workbook.

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

By default, OpenRAG performs a bounded scan of the first 32 physical rows in each detected region. It may select a later textual row when a blank row clearly separates a title or preamble from a wider table. A merged title spanning the table also selects the following row as the header. Other ambiguous layouts keep the first nonempty row to avoid silently discarding a data record.

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

A per-sheet value overrides the global row. `null` restores automatic selection for every table on that sheet. A configured row is used by the table region containing it; other detected tables on that sheet retain automatic header selection. An override that targets an absent, hidden, empty, or out-of-range worksheet fails before any content is indexed.

Blank header cells receive generated names such as `column_2`. Duplicate rendered names are made unique.

### Headerless tables

Headerless data is never guessed automatically. Supply a schema using the exact worksheet name:

```yaml
loader:
  xlsx_sheet_headerless_schemas:
    Raw data: [id, name, topic]
```

Every physical row, including the first, is then indexed as data. Metadata reports `table_header_source: configured_schema`, while `table_header_row` and `table_header_range` remain `null` so generated labels are not mistaken for source cells.

For multiple headerless tables with different schemas in one worksheet, append the one-based detected table number:

```yaml
loader:
  xlsx_sheet_headerless_schemas:
    Raw data#1: [person_id, name]
    Raw data#2: [office_id, city, service]
```

Schema width must match the visible columns in the detected table. Headerless schemas cannot be combined with a configured header row for the same worksheet. Table-specific schemas remain independent across worksheets and detected table regions.

## Formulas and numerical values

Formula cells are indexed as a cached scalar paired with inert formula text, for example:

```text
42 [formula: =SUM(B2:B10)]
```

OpenRAG never executes workbook formulas or macros. When a formula has no cached value, its expression remains searchable and a warning is logged. Cached results may be absent or stale if the workbook was not recalculated and saved by Excel, LibreOffice, or another calculation engine after its inputs changed.

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

Formula groups are scoped to the detected table and parser batch. During Markdown chunking, a group is retained only on chunks containing rows and columns intersecting its target range. A different formula interrupts the group: that exceptional cell keeps its complete expression inline and is described individually in `formula_cells` with its target coordinate, normalized pattern, semantic expression, source columns, and cache availability.

The parser still enforces the configured batch byte limit. In the unusual case where adding generalized context would make an otherwise valid batch exceed that limit, formulas in that batch remain attached to their individual cells instead of failing workbook ingestion.

Absolute references remain absolute in the normalized pattern. Same-table A1 references are mapped to headers. Cross-sheet references, structured table references, named ranges, and dynamic references that cannot be resolved safely are preserved verbatim in `unresolved_references`; OpenRAG does not guess their meaning or execute them.

Numeric values use the scalar representation returned by OpenPyXL. Excel display formatting such as currency symbols, percentage formatting, and visual rounding is not treated as stored data. If the displayed form is semantically important, store it explicitly in another cell.

## Configuration

```yaml
loader:
  xlsx_batch_size: 10000
  xlsx_header_row: null
  xlsx_sheet_header_rows: {}
  xlsx_sheet_headerless_schemas: {}
  xlsx_limits:
    max_batch_bytes: 2097152
    max_shared_strings: 250000
    max_tables: 128
```

`xlsx_batch_size` limits the number of data rows emitted in one parser batch. `xlsx_limits` accepts the positive-integer limit names listed below. Values not specified retain their defaults.

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
| Merged title above a single header row | The title is retained as table context and the following row becomes the header | No action is required when the title spans the detected table |
| Merged column-header groups | Consecutive source-backed levels are flattened and retained as hierarchy metadata; arbitrary visual layouts without merged ranges remain ambiguous | Use actual merged ranges or a single explicit header row |
| Merged row headers | Vertically merged single-column values are expanded through their covered data rows and recorded as per-row hierarchy metadata | No action is required for ordinary vertical row-header merges |
| Headerless worksheet | Without configuration, the first nonempty row is conservatively treated as the header | Supply `xlsx_sheet_headerless_schemas` or add a header row |
| Formula recalculation | Not performed | Recalculate and save the workbook before upload |
| Charts, images, comments, and shapes | Not indexed as worksheet data | Extract them through a dedicated document or media workflow |
| Password-protected or encrypted workbook | Rejected | Decrypt it before upload |
| `.xls`, `.xlsb`, `.ods`, and macro-enabled workbooks | Unsupported by the XLSX route | Convert to a plain `.xlsx` workbook or use a dedicated parser |
| Exact aggregation across very large tables or several workbooks | Semantic retrieval may not return every row needed for a calculation | Use an analytical query pipeline for exact full-dataset computation |

## Operational guidance

- Separate independent tables with at least one fully blank row or column. Add explicit headers or configure a headerless schema.
- Recalculate and save formula-heavy workbooks before uploading them.
- Keep identifiers and units in explicit cells so they remain retrievable with numerical values.
- Use a fresh partition when comparing ingestion behavior across parser versions.
- Hidden rows and columns are excluded, but workbook visibility should still not be treated as a general authorization mechanism outside OpenRAG ingestion.
