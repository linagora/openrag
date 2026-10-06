"""Connecting the CSV batches to the existing chunking strategy."""

from collections.abc import Iterator
from contextlib import closing

from core.chunking.chunking_strategy import ChunkingStrategy
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.chunk import Chunk
from core.models.document import Document, ProcessedDocument


def iter_csv_chunks(
    document: Document,
    parser: CsvParser,
    chunker: ChunkingStrategy,
) -> Iterator[list[Chunk]]:
    """Yield one list of chunks per CSV batch."""
    next_chunk_index = 0

    # close the CSV stream even if processing stops early
    with closing(parser.iter_batches(document)) as batches:
        for batch_index, batch in enumerate(batches, start=1):
            # Wrap only the current batch, not the entire CSV.
            processed = ProcessedDocument(
                document_id=document.id,
                text_blocks=[batch],
                metadata={
                    **document.metadata,
                    "csv_batch_index": batch_index,
                },
            )

            chunks = chunker.chunk(
                processed,
                partition=document.partition,
            )

            # each chunker call starts numbering at zero
            #  and we continue numbering across the entire CSV instead
            for chunk in chunks:
                chunk.chunk_index = next_chunk_index
                next_chunk_index += 1

            # release our references to the input batch
            del processed, batch

            # pause now until the consumer requests another batch
            yield chunks

            del chunks
