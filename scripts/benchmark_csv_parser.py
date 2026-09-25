# ruff: noqa: E402,I001
"""Print basic timing and memory measurements for one csv (macOS or Linux)."""

import argparse # to read options from the terminal
import asyncio
import csv # to catch the csv errors
import resource
import sys
import time
from pathlib import Path

from _bootstrap import ensure_openrag_source_path
ensure_openrag_source_path()

from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document

async def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("file", type=Path, help="csv file to measure but it will not be modified")
    cli.add_argument("--delimiter", default=",")
    args = cli.parse_args()
    if len(args.delimiter) != 1:
        cli.error("the delimiter is more than just one character")

    parser = CsvParser(delimiter=args.delimiter)
    document = Document(source_path=str(args.file))
    try:
        file_size = args.file.stat().st_size
        start_time = time.perf_counter() # the elapsed time including wait time
        start_cpu = time.process_time() # cpu time consumed consumed by this process and its threads
        result = await parser.parse(document) # running the actula parser

        cpu_seconds = time.process_time() - start_cpu
        elapsed_seconds = time.perf_counter() - start_time
        peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError, csv.Error) as error:
        cli.exit(1, f"the parsing failed: {error}\n")

    peak_mib = peak_memory / (1024 * 1024 if sys.platform == "darwin" else 1024) # macOS reports peak RSS in bytes whereas linux reports it in KiB
    print(f"File: {args.file}")
    print(f"Input size: {file_size / (1024 * 1024):.3f} MiB ({file_size} bytes)")
    print(f"Parsing time: {elapsed_seconds:.6f} seconds")
    print(f" CPU time: {cpu_seconds:.6f} seconds")
    print(f"Average CPU: {100 * cpu_seconds / elapsed_seconds:.1f}% (100% = one core)")
    print(f"Peak process memory: {peak_mib:.2f} MiB (includes both Python and  the imports)")
    print(
        f"Output: {len(result.text_blocks)} text block(s), "
        f"{sum(len(block.text) for block in result.text_blocks)} Markdown characters"
    )

if __name__ == "__main__":
    asyncio.run(main())
