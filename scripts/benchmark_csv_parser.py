# ruff: noqa: E402,I001
"""Print basic timing and memory measurements for one csv (macOS or Linux)."""

import argparse  # to read options from the terminal
import asyncio
import csv  # to catch the csv errors
import resource
import sys
import time
from contextlib import closing
from pathlib import Path

from _bootstrap import ensure_openrag_source_path

ensure_openrag_source_path()

from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document


def consume_batches(parser: CsvParser, document: Document) -> tuple[int, int]:
    """Count the output without keeping all the Markdown in memory."""
    batch_count = 0
    character_count = 0
    # Close the input file even if the consumer stops before the last batch.
    with closing(parser.iter_batches(document)) as batches:
        for batch in batches:
            batch_count += 1
            character_count += len(batch.text)
            del batch
    return batch_count, character_count


async def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("file", type=Path, help="csv file to measure but it will not be modified")
    cli.add_argument("--delimiter", default=",")
    cli.add_argument("--batch-size", type=int, default=10_000, help="Maximum data rows per batch (default: 10000)")
    args = cli.parse_args()
    if len(args.delimiter) != 1:
        cli.error("the delimiter is more than just one character")
    if args.batch_size < 1:
        cli.error("The batch size must be a positive integer")

    parser = CsvParser(delimiter=args.delimiter, batch_size=args.batch_size)
    document = Document(source_path=str(args.file))
    try:
        file_size = args.file.stat().st_size
        start_time = time.perf_counter()  # the elapsed time and including wait time
        start_cpu = time.process_time()  # cpu time consumed consumed by this process and its threads
        # running the actual parser, consuming each batch in the worker thread
        batch_count, character_count = await asyncio.to_thread(consume_batches, parser, document)
        cpu_seconds = time.process_time() - start_cpu
        elapsed_seconds = time.perf_counter() - start_time
        peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError, csv.Error) as error:
        cli.exit(1, f"the parsing failed: {error}\n")

    peak_mib = peak_memory / (
        1024 * 1024 if sys.platform == "darwin" else 1024
    )  # macOS reports peak RSS in bytes whereas linux reports it in KiB
    print(f"File: {args.file}")
    print(f"Batch size: {args.batch_size} data rows")
    print(f"Input size: {file_size / (1024 * 1024):.3f} MiB ({file_size} bytes)")
    print(f"Parsing time: {elapsed_seconds:.6f} seconds")
    print(f" CPU time: {cpu_seconds:.6f} seconds")
    print(f"Average CPU: {100 * cpu_seconds / elapsed_seconds:.1f}% (100% is one core)")
    print(f"Peak process memory: {peak_mib:.2f} MiB (includes both Python and  the imports)")
    print(
        f"Output: {batch_count} text block(s), {character_count} Markdown characters (batches discarded after counting)"
    )


if __name__ == "__main__":
    asyncio.run(main())
