# XLSX ingestion

OpenRAG indexes `.xlsx` and `.xlsm` workbooks as structured tabular content. It uses OpenPyXL in read-only mode and sends every detected table in each visible worksheet through the same bounded indexing pipeline used for CSV data. Charts placed on visible worksheets and chart sheets are indexed too, as text describing the chart and its data (see [Charts](#charts)).

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

Hidden and `veryHidden` worksheets are skipped. Inside a visible worksheet, hidden columns are excluded. Rows hidden by hand or in collapsed outline groups are excluded too, except on a worksheet whose filter is active: there, every hidden data row in a filter range is indexed (see below).

Rows hidden by an active filter are **included**. A workbook saved with a filter applied (for example `Status = Open`) still contains the rows the filter hides, and a filter is a view of the data, not a decision to remove rows from it: indexing only the visible rows would make a question such as "how many tickets are closed?" return a confident wrong answer. A row counts as filtered when the worksheet marks its filter as active (`<sheetPr filterMode="1">`) and the row lies in the data rows of the worksheet's AutoFilter range or of an Excel Table's own filter. Rows hidden outside those ranges stay excluded, and so do hidden rows on a worksheet whose filter is not marked active, since the file then does not say a filter hid them.

The worksheet context in every extracted chunk states what happened, for example `Hidden worksheet rows excluded: 2 (hidden or collapsed rows).` and `Rows hidden by an active filter are included: 15.`, and chunk metadata records `hidden_rows_skipped`, `filtered_rows_included` and `hidden_columns_skipped`. Removing hidden columns does not change physical worksheet citations.

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

An obvious merged title row immediately above a table is retained as searchable worksheet context rather than used as the column header. Context remains attached only to its originating table and worksheet. Worksheet context is repeated on every chunk of its table while it stays small. A context larger than a quarter of the chunk budget (and longer than the short pointer that replaces it) would crowd out the rows, so when the table needs splitting it is indexed once, with the table's first batch, as its own text chunks (`xlsx_content_kind: sheet_context`). The table chunks then keep the worksheet name and a line such as `Worksheet context (rows 1-3) is indexed in separate chunks.`, drop the context text from their metadata, and set `sheet_context_indexed_separately: true`. Table batches report `table_batch_index`, their one-based position within the table.

### One pass per worksheet

OpenPyXL's read-only mode parses every cell of a row, even when only a few columns are requested, so reading each table separately would parse a worksheet with N side-by-side tables N times. OpenRAG instead reads every worksheet in one forward pass per worksheet reader (cell values, plus formula text when the sheet has formulas), however its tables are arranged: stacked, side by side, or staggered. A worksheet with 256 side-by-side tables is parsed in seconds rather than minutes.

Tables are still indexed one after another, so header detection, batching, and formula handling are unchanged. Rows of a table whose turn has not come yet wait until it does:

- in memory, up to `max_spool_memory_bytes` (32 MiB). This is enough for typical worksheets, which therefore never touch the disk;
- past that, in an anonymous temporary file in `TMPDIR`, deleted when the worksheet is done;
- memory and temporary file together are bounded by `max_spool_bytes` (256 MiB).

A worksheet falls back to reading each waiting table separately, as earlier versions did, when:

- the waiting rows would exceed `max_spool_bytes`;
- the temporary file cannot be written or read back (full disk, read-only or missing `TMPDIR`);
- the rectangle around the worksheet's tables is larger than `max_sheet_area` (for example a tall Excel Table beside a wide one), because one pass would then read mostly empty cells.

The fallback is slower but produces exactly the same rows, so neither the limits nor the deployment environment change what is indexed.

### Worksheets with many blocks

Each visible worksheet is indexed as at most `max_tables` (256) separate tables; hidden worksheets are skipped and do not count. A worksheet with more separate blocks than that, such as one block per week or per store, each with its own header, is not rejected. Instead, all of its blocks are merged by column band: each block joins the most recent table that shares any of its columns, so every row is indexed.

- The first block's header names the columns; the later blocks' header rows appear as data rows.
- A merge never crosses an Excel Table and never overlaps another table; such a block stays separate.
- The worksheet context says so (`This worksheet has 300 separate blocks, more than the 256-table limit: …`), and `worksheet_blocks_merged` records the number of blocks detected (0 when nothing was merged).
- Merging happens during the preflight scan, and the `max_sheet_area` limit is checked before each block is merged, so an oversized worksheet is rejected before merging costs any work.

Only a worksheet that still has more than `max_tables` tables after merging, for example more than 256 column bands side by side, fails with `XLSX_LIMIT_EXCEEDED`.

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

## Charts

Every chart on a visible worksheet or chart sheet becomes one text block, emitted after the tables of its worksheet. It states what a reader of the chart sees, then its data:

```text
Chart: Monthly revenue
Type: clustered column chart
Worksheet: Sales, placed over E2:L16
Axes: Month (categories); EUR (values)
Data: categories Sales!$A$2:$A$13; 2025 = Sales!$B$2:$B$13; 2026 = Sales!$C$2:$C$13

| Month | 2025 | 2026 |
| --- | --- | --- |
| Jan | 100 | 200 |
```

Series sharing their categories share a table, one column per series and at most eight series per table, so a question such as "revenue in March 2026" finds the category and the value on one line. This holds across the parts of a combination chart: columns for actuals and a line for targets over the same months are one table, `| Month | Actual | Target |`, so "months below target" compares both values on each row. Category and x-value series never share a table. The category column is named after the category axis title; without one, after the header cell above the categories, but only when every series is named by the cell right above its values, in that same header row (rows work the same way, with the header to the left). Otherwise it is `Category` (`X` for scatter charts). Chunk metadata carries `xlsx_content_kind: chart`, `chart_id`, `chart_title`, `chart_types`, `chart_format`, `chart_anchor_range`, `chart_series` (each series' name, source ranges and point count; at most 64, with `chart_series_count` giving the total), `chart_point_count` and `chart_source_table_ids`, the indexed tables the chart plots.

**Chart types.** Classic charts: column and bar (clustered, stacked, 100% stacked), line, area, pie, doughnut, pie-of-pie and bar-of-pie, scatter, bubble, radar, stock, surface, 3-D variants, and combinations of these in one chart. Office 2016 charts, which Excel stores in a different format: waterfall (with the bars drawn as running totals named), treemap and sunburst (hierarchies written outer level first, such as `Europe / France / Paris`), funnel, histogram and Pareto, box and whisker, and map charts. For histograms and box plots the raw values are indexed and the chunk says that Excel bins or summarizes them.

**Where values come from.** Most writers (Excel for classic charts, LibreOffice, XlsxWriter) store the plotted values next to each reference; OpenRAG uses them. When a chart stores only references (OpenPyXL, and Excel itself for Office 2016 charts), the values are read from the worksheet cells. Nearby ranges are read together, in windows of at most 100,000 cells, so a far-apart range does not make one huge read. OpenPyXL parses a sheet from its first row on every read, so a read costs every row down to its last one (about 14 µs a row, 4 s for 300,000 rows on a laptop). All chart reads of a workbook therefore share one budget, `max_chart_scan_rows` rows parsed. The values and categories a chart needs are read first; number formats and header labels, which only refine a chart, get what is left. One exception: a chart whose dates depend on its cells' format is held in memory while that format is read, and past the held-memory bound (100,000 points) its own values are read after the lookups. When the budget runs out, a chart says how many of its ranges were not read (`chart_ranges_not_read`), apart from references that cannot be read at all (`chart_unresolved_series`); a skipped format or header lookup only leaves that chart less refined. The workbook's status reports both kinds (see Robustness). References through defined names are followed, sheet-scoped names first; Excel's internal `_xlchart` names are shown as the ranges they stand for. Unions, names defined as formulas and references to other workbooks are reported as unresolved.

**What a reader sees.** Date axes are written as ISO dates, in the workbook's 1900 or 1904 date system, not as Excel serial numbers. Percentage and currency series keep their raw values and get a `Display:` line, as tables do. A series whose chart stores no number format of its own (XlsxWriter and pandas write `General`, OpenPyXL and Excel's Office 2016 charts store none) takes its cells' format, as Excel does when drawing it: a date format for categories, a percentage or currency format for values. Values formatted as dates stay numbers. A chart that plots visible cells only (Excel's default) leaves out the cells of hidden and filtered rows and hidden columns, as Excel does when drawing it. The decision is made per point: a point whose category, value or size cell is hidden leaves every column of the table together, so categories and values stay paired even when their ranges are offset, and waterfall totals keep their names; a worksheet table, by contrast, indexes rows hidden by an active filter.

**Labels, trendlines and text on a chart.** Data labels that carry text of their own get a column next to their series, `| Month | Revenue | Revenue (data label) |`: text typed on a point ("Supplier strike"), or labels taken from a cell range (Excel 2013+, such as campaign names on scatter points). Labels that only repeat the value add nothing and are left out. A label leaves the chunk with its point when that point's row is hidden. Trendlines are named in a closing note (`Trendlines: Revenue: linear trendline.`). Text boxes drawn on the chart (`Target revised in June`) are written on a `Text on the chart:` line.

**Large charts.** At most `max_chart_points` data points are written per chart; past it, the chunk says how many were left out and adds a summary of each numeric series (minimum and maximum with their categories, and the total). A chart whose plotted ranges all lie inside indexed worksheet tables (every dimension from a cell range, every cell of it indexed, so not on a row or column hidden by hand, and no text data labels of its own) repeats at most `max_chart_points_from_tables` points: past it, the data is not indexed a second time, and the chunk keeps the summary and names the table that holds the data. When a chart is split into several chunks, the first starts with the chart's full heading and the others with a one-line context, type and worksheet first and then the title: `Chart (line chart), worksheet Sales: Gross margin trend`. That line takes at most half a chunk; a longer one is cut by word, so a very long title is shortened, not the type or worksheet. Each data table repeats its header.

**Data on hidden worksheets.** A visible chart can plot a hidden worksheet's data, and anyone who opens the file sees it in the chart. `loader.xlsx_charts` (`XLSX_CHARTS`) decides what reaches the index:

| Mode | Behavior |
| --- | --- |
| `full` (default) | Every chart with all its data. The chunk names the hidden source worksheet: `Source data: hidden worksheet 'Data'.` |
| `without_hidden_data` | Every chart that is not itself hidden in Excel (a chart hidden on its own, or inside a hidden group, is not read; in `full` it is indexed with a `Hidden in Excel.` line and `chart_hidden_in_excel: true`), with its type, axes and source references, but no value, category label, series name, title or column header from a hidden worksheet, row or column. A hidden worksheet's cells are never even read; cells in hidden rows and columns of a visible worksheet are left out of the chunk, and a cached range that cannot be matched cell by cell (several label columns) is read again from its visible cells. The chunk says `Source data: hidden worksheet 'Data'; its values are not indexed.` |
| `off` | No chart is read. Visible worksheet text boxes can still be indexed. A workbook with no other indexable content is rejected as empty. |

`chart_hidden_source_sheets` and `chart_hidden_data_withheld` record the chart policy in metadata. In `without_hidden_data`, cached data labels and text boxes are omitted when their source cells are hidden or their links cannot be resolved. Text boxes hidden in Excel are omitted too. The same text box policy applies in `off` mode. In `full`, hidden worksheet text boxes are marked `(hidden in Excel)`.

**Text boxes on worksheets.** Text boxes and other shapes holding text on a visible worksheet or chart sheet, grouped ones included, are indexed as text blocks after that sheet's charts (`xlsx_content_kind: text_box`), with the cells each one covers: `- Over F2:H4: Q3 dip: supplier strike`. A sheet can produce multiple blocks when its text exceeds the batch byte limit. Metadata gives `text_box_count` and `text_box_anchors`. The placeholder Excel stores for readers that cannot show an Office 2016 chart ("This chart isn't available in your version of Excel") is not indexed. At most 1,024 text boxes are collected per workbook, and each is cut at 4,000 characters.

**Robustness.** A chart never makes a workbook fail: a missing, malformed or oversized chart part, or a chart type that is not recognized, is skipped or reported in the chunk, and the worksheet tables are indexed as usual. Every block of the workbook, tables and loose cells included, carries the outcome, so it is visible even when no chart chunk exists: `workbook_chart_extraction` is `complete`, `partial`, `failed` (only the error type is recorded) or `off`, and `workbook_chart_issues` lists up to 16 reasons, with `workbook_chart_issue_count` giving their number. These are reasons, not a chart count: one reason, such as the chart limit, can stand for many charts. This is chunk metadata, not yet a warning on the indexing job.

**Metadata size.** Chunk metadata is stored in a 64 KiB JSON field and repeated on every chunk of a chart, so it is capped where it is made: titles at 512 characters, series names and references at 128, `chart_series` at 64 entries (`chart_series_count` gives the total) and lists such as `chart_source_table_ids` at 64. Character caps alone cannot bound bytes, since multibyte labels may be written as `\uXXXX` escapes, so a chart's metadata must also fit 32 KiB in that worst encoding, half the field, the rest left to the chunk's other keys: series entries are dropped from the last, then the context line and the title are shortened. `chart_metadata_truncated` says when anything was cut; the chunk text keeps the full labels.

Chart caches with excessively sparse point indexes are skipped to prevent a small XML part from causing a large allocation. Worksheet references for charts are read in bounded windows, so distant references do not expand into one large rectangular read.

## Configuration

```yaml
loader:
  xlsx_batch_size: 10000
  # full, without_hidden_data or off; see Charts
  xlsx_charts: full
  xlsx_limits:
    max_batch_bytes: 2097152
    max_shared_strings: 1000000
    max_tables: 256
    max_chart_points: 5000
```

`xlsx_charts` selects the chart mode and can also be set with `XLSX_CHARTS`; unknown modes are rejected when the configuration loads. `xlsx_batch_size` limits the number of data rows emitted in one parser batch and can also be set with `XLSX_BATCH_SIZE` (see the [environment reference](/openrag/documentation/env_vars/#excel-workbook-ingestion)). A batch may flush earlier to stay within `max_batch_bytes`. Parser batches are then split into retrieval chunks according to the configured chunk token budget; batch size is not chunk size.

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
| `max_metadata_bytes` | Total size of the parts the reader loads whole: workbook, styles, theme, document properties, worksheet relationships, and chart sheets with their drawings, charts, and images. Pivot caches, comments, and media are never loaded and only count toward the archive limits; worksheet drawings and charts are read by chart extraction only, each bounded by `max_chart_part_bytes` | 8 MiB |
| `max_shared_strings_bytes` | Shared-string XML. OpenPyXL keeps the whole table in memory, about 4–5 times this size, so this is the limit that bounds that memory | 32 MiB |
| `max_shared_strings` | Shared-string entries. Distinct text values in a workbook usually reach the byte limit first; 60,000 rows of 5 unique text columns is 300,000 entries and about 11 MB of XML | 1,000,000 |
| `max_sheets` | Worksheets, including hidden sheets | 128 |
| `max_rows` | Row coordinate of a cell with content | 1,048,576 |
| `max_columns` | Column coordinate of a cell with content | 4,096 |
| `max_cells` | Physical cells across worksheets, empty styled cells included | 5,000,000 |
| `max_sheet_area` | Traversal rectangle from `A1` to the last content-bearing cell or a declared Excel Table's bottom-right corner, including leading space and gaps | 5,000,000 positions |
| `max_tables` | Tables per visible worksheet, and Excel Tables per worksheet. Past it, detected blocks sharing columns are merged instead of rejecting the workbook | 256 |
| `max_merged_ranges` | Merged-cell ranges across the workbook | 100,000 |
| `max_cell_chars` | Characters in one cell | 32,767 |
| `max_batch_bytes` | Rendered batch size | 2 MiB |
| `max_spool_bytes` | Memory plus temporary-file space per worksheet for rows of tables waiting for their turn in the single worksheet pass. Past it, waiting tables are read separately; output is unchanged | 256 MiB |
| `max_spool_memory_bytes` | Part of `max_spool_bytes` kept in memory before the temporary file is used | 32 MiB |
| `max_charts` | Charts read across the workbook. Later charts are skipped, never rejected | 256 |
| `max_chart_part_bytes` | One drawing or chart part, read whole when the workbook is parsed. Cached values make charts large: 10 series of 5,000 points is about 4 MB. A larger part is skipped | 16 MiB |
| `max_chart_bytes` | Separate workbook-wide byte budgets for drawing discovery and chart content. Parts exceeding a budget are skipped without rejecting the workbook | 64 MiB each |
| `max_chart_points` | Data points written per chart, and cells read per range of a chart without stored values. The chunk says when points were left out | 5,000 |
| `max_chart_points_from_tables` | Data points a chart repeats when its data is already indexed as worksheet tables. Past it, the chart keeps a summary and names the table | 200 |
| `max_chart_scan_rows` | Worksheet rows the chart cell reads of a workbook may parse in total. Each read parses its sheet from the first row, so this bounds their time; ranges past it are reported, not read | 2,000,000 |

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
| Row hidden by hand or in a collapsed group inside an active filter's range | A workbook does not record why a row is hidden (LibreOffice writes `hidden="true" outlineLevel="0"` for a filtered row and a row hidden by hand alike), so every hidden data row in the range is treated as filtered and indexed. Filter criteria are not re-evaluated | Delete the row, or move it outside the filtered range, if it must not be indexed |
| Rows in a collapsed outline group outside any active filter range | Excluded, like rows hidden by hand: folded detail is a different intent from a filter | Expand the group before saving if those rows should be indexed |
| More than 256 blocks in one worksheet | Blocks sharing columns are merged into one table per column band; later header rows appear as data rows | Spread the blocks over several worksheets, or combine them into one table with a column naming the block (week, store), when that matters |
| Formula recalculation | Not performed | Recalculate and save the workbook before upload |
| Images, comments, and shapes | Not indexed | Extract them through a dedicated document or media workflow |
| Chart decorations | Error bars, colors and labels that only repeat a value are not indexed; text data labels, trendline types and text drawn on a chart are | No action is required for the plotted data |
| Data labels on a chart whose cached points cannot be matched to cells, with hidden rows in the plotted range | Those labels are left out rather than risk landing on the wrong point | Unhide the rows, or store full caches (save the workbook in Excel) |
| Sparklines, pictures and cell comments | Not indexed by this chart extraction | Repeat important notes in cells or text boxes |
| Chart over data on a hidden worksheet | Indexed with its data in the default `full` chart mode, with a note naming the hidden worksheet | Set `XLSX_CHARTS=without_hidden_data`, or remove the data, if hidden worksheets must stay out of the index |
| Chart references that are unions, formulas or other workbooks | The chart is indexed without those values, and the chunk says how many series could not be read | Plot single ranges of the same workbook |
| Chart without a category axis title whose series are not named from a header row | The category column is labelled `Category` rather than guessed | Give the category axis a title, or name the series from the header row above the data |
| Chart value series formatted as dates in the cells, with no format stored in the chart | The values stay Excel serial numbers | Plot dates as categories, or store a date format in the chart |
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
- Charts are indexed with their data by default, including data they plot from hidden worksheets. Use `XLSX_CHARTS=without_hidden_data` to keep hidden data out while still indexing the charts, or `off` to skip charts.
- Hidden columns are excluded. Hidden rows are excluded too, except on a worksheet whose filter is active: there, every hidden data row in the worksheet's AutoFilter range or in an Excel Table's filter range is indexed, whether the filter, a collapsed group or the user hid it. Workbook visibility should never be treated as an authorization mechanism: remove data that must not be indexed instead of hiding it.
