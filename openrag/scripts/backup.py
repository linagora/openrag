#!/usr/bin/env python3

import json
import os
import sys
from typing import IO, Any

from pymilvus import Collection, connections
from services.persistence.schema import files as files_table
from services.persistence.schema import partitions as partitions_table
from sqlalchemy import create_engine, select
from utils.logger import get_logger


def _list_partitions(conn) -> list[dict]:
    rows = conn.execute(select(partitions_table)).all()
    return [{"partition": r.partition, "created_at": r.created_at.isoformat()} for r in rows]


def _list_partition_files(conn, partition_name: str, limit: int | None = None) -> dict:
    q = select(files_table).where(files_table.c.partition_name == partition_name)
    if limit is not None:
        q = q.limit(limit)
    rows = conn.execute(q).all()
    file_list = []
    for r in rows:
        entry = {"file_id": r.file_id, "partition": r.partition_name}
        if r.relationship_id is not None:
            entry["relationship_id"] = r.relationship_id
        if r.parent_id is not None:
            entry["parent_id"] = r.parent_id
        entry.update(r.file_metadata or {})
        file_list.append(entry)
    return {"files": file_list}


def dump_rdb_part(
    out_fh: IO[str],
    conn,
    partitions: dict[str, dict[str, Any]],
    logger: Any,
    verbose: bool = False,
):
    """
    Dumps relational DB data into the backup file:
      - writes only requested partitions
      - lines are groupped by partition

    Parameters:
        out_fh:      File handle opened for writing.
        pfm:         OpenRAG PartitionFileManager
        partitions:  Mapping of partition name -> partition metadata.
        logger:      Logger instance.
        verbose:     Be verbose.

    Returns:
        None
    """
    for part_name in sorted(partitions):
        # Header
        out_fh.write("rdb\n")
        if verbose:
            logger.info("Writing rdb data")

        # Partition details
        out_fh.write(
            json.dumps(
                {"name": part_name, "created": partitions[part_name]["created_at"]},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

        try:
            files = _list_partition_files(conn, part_name)
        except Exception as e:
            logger.error(f"Failed while requesting the list of files in partition '{part_name}'\n{e}")
            raise

        files["files"].sort(key=lambda v: v["file_id"])

        for f in files["files"]:
            f.pop("partition", None)
            out_fh.write(json.dumps(f, ensure_ascii=False, sort_keys=True) + "\n")

        # Separator
        out_fh.write("\n")

        if verbose:
            logger.info(f"Partition '{part_name}' - {len(files['files'])} files")


def dump_vdb_part(
    out_fh: IO[str],
    collection: Collection,
    partitions: dict[str, dict[str, Any]],
    logger: Any,
    batch_size: int = 1024,
    verbose: bool = False,
):
    """
    Dumps vector DB data into the backup file:
      - writes one chunk per line
      - writes only chunks belonging to partitions requested
      - no particular order guaranteed

    Parameters:
        out_fh:      File handle opened for writing backup data.
        collection:  Milvus collection to query from.
        partitions:  Mapping of partition name -> partition metadata.
        logger:      Logger instance.
        batch_size:  Number of chunks per batch.
        verbose:     Be verbose.

    Returns:
        None
    """
    try:
        collection.load()
    except Exception as e:
        logger.error(f"Failed while loading Milvus collection: {e}")
        raise

    try:
        iterator = collection.query_iterator(
            batch_size=batch_size,  # size of each batch
            output_fields=["*"],  # all fields
        )
    except Exception as e:
        logger.error(f"Failed while trying to obtain Milvus collection iterator: {e}")
        raise

    out_fh.write("vdb\n")
    if verbose:
        logger.info("Writing vdb data")
        cnt = 0

    while True:
        try:
            batch = iterator.next()
        except Exception as e:
            logger.error(f"iterator.next() failed with: {e}")
            raise

        if not batch:  # no more data
            break

        for entity in batch:
            if entity["partition"] in partitions:
                entity.pop("_id", None)
                out_fh.write(json.dumps(entity, ensure_ascii=False, sort_keys=True) + "\n")
                if verbose:
                    cnt += 1

    if verbose:
        logger.info(f"{cnt} chunks written")


def open_output_file(file_name: str, logger: Any) -> IO[str]:
    """
    Opens output file for writing

    Parameters:
        file_name:  Path to the output file or '-' for STDOUT
        logger:     Logger for status and error reporting.

    Returns:
        file object:  Opened file handle in text mode.
    """
    if file_name in ["-"]:
        return sys.stdout

    if os.path.isfile(file_name):
        raise Exception(f"File '{file_name}' already exists.")

    try:
        if file_name.endswith(".xz"):
            import lzma

            return lzma.open(file_name, "wt", encoding="utf-8", preset=9 | lzma.PRESET_EXTREME)
        else:
            return open(file_name, "w", encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed while opening file '{file_name}' for writing:\n" + str(e))
        raise


def main():
    """
    Main entry point:
      - Parses CLI arguments.
      - Loads OpenRAG configuration.
      - Connects to RDB (PostgreSQL) and VDB (Milvus).
      - Retrieves and filters partitions.
      - Dumps RDB and VDB data.

    Parameters:
        None (arguments are parsed from sys.argv)

    Returns:
        int: Exit code (0 on success, non-zero on failure).
    """

    def load_openrag_config(logger):
        """
        Loads OpenRAG configuration.

        Parameters:
            logger: Logger instance.

        Returns:
            tuple: (RDBConfig, VectorDBConfig) Pydantic config models.
        """
        from config import load_config

        try:
            config = load_config()
        except Exception as e:
            logger.error(f"Failed while trying to obtain OpenRAG config: {e}")
            raise

        return config.rdb, config.vectordb

    # Arguments and configs
    import argparse

    parser = argparse.ArgumentParser(description="OpenRAG backup tool")
    parser.add_argument("-i", "--include-only", nargs="*", help="Include only listed partitions")
    parser.add_argument("-o", "--output", required=True, help="Output file name (- for STDOUT)")
    parser.add_argument(
        "-b",
        "--batch-size",
        default=1024,
        type=int,
        help="Batch size used to iterate Milvus",
    )
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Be verbose")

    args = parser.parse_args()

    logger = get_logger()

    rdb, vdb = load_openrag_config(logger)

    if args.verbose:
        logger.info(f"rdb @ {rdb.host}:{rdb.port} | vdb @ {vdb.host}:{vdb.port} | collection: {vdb.collection_name}")

    # List existing partitions
    database_url = (
        f"postgresql://{rdb.user}:{rdb.password}@{rdb.host}:{rdb.port}/partitions_for_collection_{vdb.collection_name}"
    )
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            existing_partitions = {item["partition"]: item for item in _list_partitions(conn)}
    except Exception as e:
        logger.error(f"Failed while accessing catalog database at {rdb.host}:{rdb.port}\n{e}")
        raise

    if args.include_only:
        partitions = {}
        for part_name in args.include_only:
            if part_name not in existing_partitions:
                logger.error(f'Partition "{part_name}" has not been found.')
                return 1
            else:
                partitions[part_name] = existing_partitions[part_name]
    else:
        partitions = existing_partitions

    if 0 == len(partitions):
        logger.error("No partitions meet given conditions.")
        return 1

    if args.verbose:
        partitions_str = ", ".join(partitions)
        logger.info(f"partitions: {partitions_str}")

    # Connect to Milvus
    try:
        connections.connect("default", host=vdb.host, port=vdb.port)
    except Exception as e:
        logger.error(f"Can't connect to Milvus at {vdb.host}:{vdb.port}\n{e}")
        raise

    try:
        vdb_collection = Collection(vdb.collection_name)
    except Exception as e:
        logger.error(f"Can't access Milvus collection {vdb.collection_name} at {vdb.host}:{vdb.port}\n{e}")
        raise

    try:
        with open_output_file(args.output, logger) as out_fh:
            with create_engine(database_url).connect() as conn:
                # Dump data from RDB (one line per document)
                dump_rdb_part(out_fh, conn, partitions, logger, args.verbose)

            # Dump data from VDB (one line per chunk)
            dump_vdb_part(
                out_fh,
                vdb_collection,
                partitions,
                logger,
                args.batch_size,
                args.verbose,
            )

            out_fh.flush()
    except Exception as e:
        logger.exception("ERROR: " + str(e))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
