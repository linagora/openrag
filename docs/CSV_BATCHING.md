# CSV batching

The parser can produce Markdown tables one batch at a time. `batch_size` is the
maximum number of **data records** per table; it defaults to 10,000. The repeated
header and Markdown separator do not count toward this limit. A quoted cell
containing several lines still belongs to one CSV record. Blank records are skipped.

## Files to understand

| File (relative to the repository root) | Purpose |
| --- | --- |
| `openrag/core/indexing/parsers/tabular/csv_parser.py` | Reads CSV records and yields a `TextBlock` for each batch. |
| `tests/unit/core/indexing/parsers/test_csv_parser.py` | Checks CSV behavior, batch boundaries, lazy parsing, stream cleanup and configuration. |
| `scripts/benchmark_csv_parser.py` | Consumes and discards each batch to measure parsing without accumulating the output. |
| `scripts/demo_csv_parser.py` | Collects all batches and exports readable Markdown and a `ProcessedDocument` JSON file. |

## Two ways to call the parser

`await parser.parse(document)` returns one `ProcessedDocument`. Its `text_blocks`
list contains every batch, and its identity and metadata come from the input
document. This preserves the existing parser interface, but holds all output in RAM.

`parser.iter_batches(document)` is a synchronous generator. Each request for the
next item reads enough CSV records to produce the next table. To save memory,
process a table and release it before requesting another. Use a file-backed
`Document(source_path=...)` to avoid loading the entire input into RAM first.

```python
from contextlib import closing

parser = CsvParser(batch_size=1_000)
document = Document(source_path="customers.csv")

with closing(parser.iter_batches(document)) as batches:
    for batch in batches:
        # Replace this with work that finishes before requesting another batch.
        print(len(batch.text))
        del batch
```

`closing` closes the generator and its file even if the consumer breaks out of the
loop or raises an error. In an async application, run this synchronous consuming
function through `asyncio.to_thread`, as the benchmark does.

For five records and a batch size of two, the output is three tables with 2, 2,
and 1 data records. Every table repeats the same header. An exact multiple produces
no extra empty table. A header-only input produces one header-only table; an empty
input produces no tables.

## Run the checks

Run these commands from the repository root:

```sh
cd /Users/nthn/Developer/linagora/openrag
venv/bin/python -m pytest -v tests/unit/core/indexing/parsers/test_csv_parser.py
```

Tests 01–11 cover the earlier parsing behavior. Test 12 checks different batch
boundaries without lost or duplicated rows. Test 13 proves a later malformed
record is checked only when the caller asks for more output. Test 14 checks file
cleanup, test 15 rejects invalid sizes, and test 16 checks the async compatibility
interface. Some tests run several parameter combinations, so pytest reports more
cases than named test functions.

## Measure memory and time

```sh
venv/bin/python scripts/benchmark_csv_parser.py examples/csv_parser/customers/customers-2000000.csv --batch-size 10000
venv/bin/python scripts/benchmark_csv_parser.py examples/csv_parser/customers/customers-2000000.csv --batch-size 1000
```

These example datasets are local and are not included in Git. Substitute another
CSV path if necessary. Each command runs a fresh process. Compare the same file
with different batch sizes; a smaller batch trades more tables and repeated
headers for less temporary memory. The output character count includes those
repeated headers and can therefore change with the batch size.

The benchmark measures CSV parsing and Markdown generation, without saving the
output or running chunking, embedding or database writes. Peak process memory
includes Python and imports. Average CPU is CPU time divided by elapsed time,
multiplied by 100; 100% corresponds approximately to one core. Single runs are
rough measurements, especially for tiny files.

## See the actual tables

```sh
venv/bin/python scripts/demo_csv_parser.py examples/csv_parser/people.csv --batch-size 2
```

This writes `people.parsed.md` and `people.parsed.json` alongside the input and
prints the Markdown. The JSON contains the full `ProcessedDocument`, with one
entry in `text_blocks` per batch. Existing output files are overwritten on a
successful run; the CSV is unchanged. Use a small file for this demonstration:
the exporter collects and serializes the entire result in memory.

## What remains for OpenRAG integration

This is parser-level batching, not a complete streaming indexing pipeline.
The existing `parse()` interface collects all output. The recursive chunker can
also join document blocks into one string. Passing many blocks through that
existing route does not provide bounded memory across indexing.

The next integration step needs a consumer that processes each batch through
chunking, embedding and storage before accepting more work, or uses a bounded
queue. It must preserve document identity and chunk ordering across batches,
limit concurrent indexing, and decide how to handle a later parsing failure
after earlier batches have already been stored. Streaming may emit valid batches
before discovering an invalid record; it does not validate the whole file first.

The batch size is currently configured through the constructor or these scripts'
`--batch-size` option, not a server configuration setting. Upload detection and
dispatcher wiring are separate work. A parser batch is also not a retrieval chunk:
each table may still need to be split for embedding.

A row limit is not a byte limit. Very wide rows or large cells can still consume
substantial memory, and Python's CSV reader has a field-size limit. These tests
do not establish a hard RAM ceiling or production concurrency capacity.
