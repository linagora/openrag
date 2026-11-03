"""
Add the filename to chunks already indexed
==========================================
Some chunks don't contain their filename, so a question like "what is report.pdf
about?" can't find them. This script adds a ``[Source: <filename>]`` line at the
start of those chunks.

Nothing is re-parsed or re-embedded. Milvus updates BM25 from the new text, and a
question naming a file is a keyword search, so that is enough.

Chunks that already name their file are skipped, so it is safe to run again. Don't
run it during an embedder swap, which rewrites the same rows.

This is a data fix, so it isn't a numbered migration: those force every deployment
to run them.

Usage (from infra/compose, with DC and SVC set as in the upgrade guide):

    $DC run --no-deps --rm --entrypoint "" "$SVC" \\
        uv run --no-dev python services/persistence/migrations/milvus/backfill_chunk_headers.py --dry-run

``--dry-run`` writes nothing, ``--partition <name>`` limits it to one partition
(repeatable), and ``--revert`` removes what it added.
"""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Any

from core.chunking.recursive import source_header
from core.config import load_config
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from pymilvus import MilvusClient, MilvusException
from services.storage.milvus_store import MAX_SECTION_ID, SECTION_ID_KEYS, _changed_by_float64

logger = get_logger()

# A row can reach about 130 KB, so a page stays well below Milvus's message limit.
DEFAULT_BATCH_SIZE = 200

# What contextualization writes, the same in every release so far. Matching the whole
# form means a body that merely quotes "* filename: " still gets the header.
_FILENAME_LINE = re.compile(r"^\* filename: .*\n\n\[CHUNK_START\]", re.MULTILINE)

# What Milvus returns for a row it refuses: a deleted chunk, or one now too long.
_ROW_REFUSED_CODE = 1100

# 1100 is Milvus's generic invalid-parameter code, so it can also mean the collection
# refuses every write. Stop at this many refusals with nothing written, rather than
# trying each chunk of the collection.
_MAX_REFUSED_WITH_NOTHING_WRITTEN = DEFAULT_BATCH_SIZE

_OUTPUT_FIELDS = ["text", "file_id", "$meta"]
_SAMPLE_SIZE = 3
_PROGRESS_EVERY_PAGES = 50


def _is_body(content: Any, body: str) -> bool:
    """Whether *content* is the chunk's body, as a chunker stored it. Storing a row too
    big for Milvus cuts either one short, so one being the start of the other counts."""
    return isinstance(content, str) and (body.startswith(content) or content.startswith(body))


def _needs_filename(row: dict[str, Any], header_for: Callable[[dict[str, Any]], str | None]) -> bool:
    text = row.get("text") or ""
    header = row.get("header")
    # Upload metadata can use the names ``header`` and ``content`` too, so they only
    # count when they match the text. ``content`` matching all of it is structured_section
    # with ``prepend_heading_path`` off, which was asked not to carry a preamble.
    if (header and text.startswith(f"{header}\n\n")) or _is_body(row.get("content"), text):
        return False
    own_header = header_for(row)
    if own_header and text.startswith(f"{own_header}\n\n"):
        return False
    return not _FILENAME_LINE.search(text)


def _folded_section_ids(row: dict[str, Any]) -> dict[str, int]:
    """Milvus would round these IDs, so fold them as ``write_vectors`` does. Chunks
    keep their neighbours."""
    return {
        key: row[key] & MAX_SECTION_ID
        for key in SECTION_ID_KEYS
        if isinstance(row.get(key), int) and not 0 <= row[key] <= MAX_SECTION_ID
    }


def _rounded_keys(row: dict[str, Any]) -> set[str]:
    return {
        key
        for key, value in row.items()
        if key not in ("_id", "file_id", *SECTION_ID_KEYS) and _changed_by_float64(value)
    }


def _with_header(row: dict[str, Any], header: str, length_function: Callable[[str], int]) -> dict[str, Any]:
    entity = {"_id": row["_id"], "text": f"{header}\n\n{row.get('text') or ''}", **_folded_section_ids(row)}
    # A ``header`` already here came from upload metadata, so it stays.
    if not row.get("header"):
        entity["header"] = header
    if isinstance(row.get("token_count"), int):
        entity["token_count"] = length_function(entity["text"])
    return entity


def _without_header(
    row: dict[str, Any], header_for: Callable[[dict[str, Any]], str | None], length_function: Callable[[str], int]
) -> dict[str, Any] | None:
    """Undo ``_with_header``. Chunkers store the body in ``content``, so a chunk whose
    ``content`` is the text after the header wasn't changed by this script."""
    text = row.get("text") or ""
    # Every chunk this script changed has a ``header``: the line it added, or an upload's value.
    if not row.get("header"):
        return None
    for header in (row.get("header"), header_for(row)):
        prefix = f"{header}\n\n"
        if not header or not text.startswith(prefix) or _is_body(row.get("content"), text[len(prefix) :]):
            continue
        entity = {"_id": row["_id"], "text": text[len(prefix) :], **_folded_section_ids(row)}
        if row.get("header") == header:
            entity["header"] = None
        if isinstance(row.get("token_count"), int):
            entity["token_count"] = length_function(entity["text"])
        return entity
    return None


def _pages(client: MilvusClient, collection_name: str, filter_expr: str, batch_size: int) -> Iterator[list[dict]]:
    iterator = client.query_iterator(
        collection_name=collection_name,
        filter=filter_expr,
        batch_size=batch_size,
        output_fields=_OUTPUT_FIELDS,
    )
    try:
        while True:
            page = iterator.next()
            if not page:
                return
            yield page
    finally:
        iterator.close()


def _write(client: MilvusClient, collection_name: str, entities: list[dict[str, Any]]) -> int:
    """Upsert *entities* and return how many Milvus refused.

    One refused row fails its whole batch, so split the batch to save the good rows.
    Any other error, such as an outage, stops the run: splitting would only repeat
    it, with each call retrying for minutes. The run can be started again.
    """
    try:
        client.upsert(collection_name=collection_name, data=entities, partial_update=True)
        return 0
    except MilvusException as exc:
        if exc.code != _ROW_REFUSED_CODE:
            raise
        if len(entities) == 1:
            logger.bind(chunk_id=entities[0]["_id"]).warning(f"Chunk left as it was: {exc}")
            return 1
    middle = len(entities) // 2
    return _write(client, collection_name, entities[:middle]) + _write(client, collection_name, entities[middle:])


def _unknown_partitions(client: MilvusClient, collection_name: str, partitions: list[str]) -> list[str]:
    return [
        partition
        for partition in partitions
        if not client.query(
            collection_name=collection_name,
            filter=f"partition == {json.dumps(partition, ensure_ascii=False)}",
            output_fields=["_id"],
            limit=1,
        )
    ]


def run(
    client: MilvusClient,
    collection_name: str,
    *,
    length_function: Callable[[str], int],
    chunk_size: int,
    partitions: list[str] | None = None,
    revert: bool = False,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Counter:
    """Add the filename to the chunks of *partitions* (all when None), or remove it with *revert*."""
    stats: Counter = Counter()
    # The same header as new chunks. Most chunks of a file share a name, so count its tokens once.
    header_of_name = lru_cache(maxsize=1024)(lambda name: source_header(name, chunk_size, length_function))

    def header_for(row: dict[str, Any]) -> str | None:
        return header_of_name(str(row["filename"])) if row.get("filename") else None

    rounded: dict[str, set[str]] = {}
    filter_expr = f"partition in {json.dumps(partitions, ensure_ascii=False)}" if partitions else ""

    for number, page in enumerate(_pages(client, collection_name, filter_expr, batch_size), start=1):
        stats["scanned"] += len(page)
        entities: list[dict[str, Any]] = []
        for row in page:
            if revert:
                entity = _without_header(row, header_for, length_function)
                if entity is None:
                    stats["unchanged"] += 1
                    continue
            elif not _needs_filename(row, header_for):
                stats["unchanged"] += 1
                continue
            elif not (header := header_for(row)):
                stats["no_filename"] += 1
                continue
            else:
                entity = _with_header(row, header, length_function)
            entities.append(entity)
            keys = _rounded_keys(row)
            if keys:
                rounded.setdefault(str(row.get("file_id")), set()).update(keys)

        if dry_run:
            for entity in entities[: max(0, _SAMPLE_SIZE - stats["changed"])]:
                logger.info(f"  chunk {entity['_id']} would start with: {entity['text'][:100]!r}")
            stats["changed"] += len(entities)
        elif entities:
            refused = _write(client, collection_name, entities)
            stats["refused"] += refused
            stats["changed"] += len(entities) - refused
            if not stats["changed"] and stats["refused"] >= _MAX_REFUSED_WITH_NOTHING_WRITTEN:
                raise RuntimeError(
                    f"Milvus refused all {stats['refused']} chunks tried so far, so it may be refusing every write. "
                    "Stopping: see the warnings above."
                )
        if number % _PROGRESS_EVERY_PAGES == 0:
            logger.info(f"  {stats['scanned']} chunk(s) scanned, {stats['changed']} changed")

    if rounded:
        logger.bind(rounded={file_id: sorted(keys) for file_id, keys in rounded.items()}).warning(
            "Rewriting these files' chunks rounds integers beyond ±2**53 in their metadata. "
            "PostgreSQL keeps the exact values."
        )
    if not dry_run and stats["changed"]:
        client.flush(collection_name)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Add the filename to chunks already indexed.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only, make no changes")
    parser.add_argument("--revert", action="store_true", help="Remove what this script added")
    parser.add_argument(
        "--partition",
        action="append",
        default=None,
        help="Only this partition; repeat for several (default: all partitions)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Chunks read and written at a time (default: {DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error(f"--batch-size must be at least 1, got {args.batch_size}.")

    cfg = load_config()
    uri = f"http://{cfg.vectordb.host}:{cfg.vectordb.port}"
    collection_name = cfg.vectordb.collection_name

    logger.info(f"Connecting to Milvus at {uri}, collection='{collection_name}'")
    client = MilvusClient(uri=uri)
    if not client.has_collection(collection_name):
        logger.error(f"Collection '{collection_name}' does not exist. Aborting.")
        sys.exit(1)

    unknown = _unknown_partitions(client, collection_name, args.partition or [])
    if unknown:
        logger.error(f"No chunks found in partition(s) {unknown}. Nothing was changed. Aborting.")
        sys.exit(1)

    stats = run(
        client,
        collection_name,
        length_function=get_num_tokens(),
        chunk_size=cfg.chunker.chunk_size,
        partitions=args.partition,
        revert=args.revert,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )
    logger.info(
        f"{'Would change' if args.dry_run else 'Changed'} {stats['changed']} of {stats['scanned']} chunk(s): "
        f"{stats['unchanged']} needed nothing, {stats['no_filename']} have no filename, "
        f"{stats['refused']} refused by Milvus."
    )
    if stats["refused"]:
        logger.error(
            "Some chunks were refused: see the warnings above. Running this again retries them, "
            "but a chunk too long to take the header is refused every time and needs re-indexing."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
