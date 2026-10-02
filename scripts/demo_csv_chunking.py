# ruff: noqa: E402,I001
"""Prints CSV chunks progressively without collecting the whole result."""

import argparse
import csv
from contextlib import closing
from pathlib import Path

from _bootstrap import ensure_openrag_source_path

ensure_openrag_source_path()

from core.chunking.structured_section import StructuredSectionChunker
from core.indexing.csv_chunking import iter_csv_chunks
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("file", type=Path)
    cli.add_argument("--batch-size", type=int, default=10_000)
    cli.add_argument("--delimiter", default=",")
    cli.add_argument("--chunk-words", type=int, default=100)
    args = cli.parse_args()

    if args.batch_size < 1 or args.chunk_words < 1:
        cli.error("Batch size and chunk words must be positive")
    if len(args.delimiter) != 1:
        cli.error("The delimiter must be one character")

    document = Document(
        source_path=str(args.file),
        filename=args.file.name,
        metadata={"source": args.file.name},
    )

    parser = CsvParser(
        delimiter=args.delimiter,
        batch_size=args.batch_size,
    )

    # this demonstration uses word counts to stay simple
    # production uses its configured tokenizer and chunker settings
    chunker = StructuredSectionChunker(
        chunk_size=args.chunk_words,
        min_tokens=0,
        max_tokens=args.chunk_words,
        inline_threshold=0,
        length_function=lambda text: len(text.split()),
    )

    total_chunks = 0

    try:
        with closing(
            iter_csv_chunks(document, parser, chunker)
        ) as groups:
            for batch_index, chunks in enumerate(groups, start=1):
                print(
                    f"\n=== CSV batch {batch_index}: "
                    f"{len(chunks)} chunks ==="
                )

                for chunk in chunks:
                    print(f"\nChunk {chunk.chunk_index}")
                    print(f"Document: {chunk.document_id}")
                    print(f"Metadata: {chunk.metadata}")
                    print(chunk.text)

                total_chunks += len(chunks)

                # keep none of the last chunk nor this batch's list
                if chunks:
                    del chunk
                del chunks

    except (OSError, ValueError, csv.Error) as error:
        cli.exit(1, f"Processing failed: {error}\n")
    print(f"\nTotal chunks: {total_chunks}")


if __name__ == "__main__":
    main()
