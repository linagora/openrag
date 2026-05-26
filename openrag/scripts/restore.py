#!/usr/bin/env python3

import json
import sys
import time
from typing import IO, Any

from pymilvus import MilvusClient
from services.persistence.schema import files as files_table
from services.persistence.schema import partition_memberships
from services.persistence.schema import partitions as partitions_table
from services.persistence.schema import users as users_table
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from utils.logger import get_logger


def _list_partitions(conn) -> list[dict]:
    rows = conn.execute(select(partitions_table)).all()
    return [{"partition": r.partition, "created_at": r.created_at.isoformat()} for r in rows]


def _add_file_to_partition(conn, file_id: str, partition: str, file_metadata: dict, user_id: int) -> bool:
    existing = conn.execute(
        select(files_table.c.id).where(
            files_table.c.file_id == file_id,
            files_table.c.partition_name == partition,
        )
    ).first()
    if existing:
        return False

    partition_exists = conn.execute(
        select(partitions_table.c.id).where(partitions_table.c.partition == partition)
    ).first()
    if not partition_exists:
        conn.execute(partitions_table.insert().values(partition=partition))
        conn.execute(
            pg_insert(partition_memberships)
            .values(partition_name=partition, user_id=user_id, role="owner")
            .on_conflict_do_nothing()
        )

    conn.execute(
        files_table.insert().values(
            file_id=file_id,
            partition_name=partition,
            file_metadata=file_metadata,
            created_by=user_id,
            relationship_id=file_metadata.get("relationship_id"),
            parent_id=file_metadata.get("parent_id"),
        )
    )
    conn.execute(
        users_table.update().where(users_table.c.id == user_id).values(file_count=users_table.c.file_count + 1)
    )
    conn.commit()
    return True


def read_rdb_section(
    fh: IO[str],
    conn,
    include_only: list[str] | None,
    added_documents: dict[str, set[str]],
    existing_partitions: dict[str, Any],
    logger: Any,
    user_id: int,
    verbose: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Reads and restores a relational database (RDB) section from the backup file.

    Parameters:
        fh:                   Backup file handle (already open for reading).
        pfm:                  Manager for handling partition operations.
        include_only:         List of partitions to include; if None, all are processed.
        added_documents:      Dict mapping added partitions to sets of added file IDs.
        existing_partitions:  Dict of already existing partitions to avoid duplicates.
        logger:               Logger for status and error reporting.
        user_id:              User id to pass to PartitionFileManager
        verbose:              If True, logs additional info.
        dry_run:              If True, no changes are made to the database.
    """
    try:
        line = next(fh)
        part = json.loads(line)
    except Exception as e:
        logger.exception(f"Failed while parsing the following json:\n{line}\n" + str(e))
        raise

    if part["name"] in existing_partitions:
        raise Exception(f'Partition "{part["name"]}" already exists')

    if verbose:
        logger.info(f'Read rdb section | partition="{part["name"]}"')

    for line in fh:
        line = line.strip()

        if 0 == len(line):
            break

        if include_only is not None and len(include_only) > 0 and part["name"] not in include_only:
            continue

        try:
            doc = json.loads(line)
        except Exception as e:
            logger.exception(f"Failed while parsing the following json:\n{line}\n" + str(e))
            raise

        if not dry_run:
            try:
                res = _add_file_to_partition(conn, doc["file_id"], part["name"], doc, user_id)
            except Exception as e:
                logger.exception(
                    f"{type(e)} in _add_file_to_partition({doc['file_id']}, {part['name']}, ...)\n" + str(e)
                )
                raise
        else:
            res = True

        if res:
            if part["name"] not in added_documents:
                added_documents[part["name"]] = set()
            added_documents[part["name"]].add(doc["file_id"])
        else:
            logger.error(f"Can't add file {doc['file_id']} to partition {part['name']}")


def insert_into_vdb(
    client: MilvusClient,
    collection_name: str,
    batch: list,
    logger: Any,
    verbose: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Inserts a batch of chunks into the vector database.

    Parameters:
        client:           Milvus client instance.
        collection_name:  Target collection name.
        batch:            List of chunks to insert.
        logger:           Logger for status and error reporting.
        verbose:          If True, logs additional info.
        dry_run:          If True, skips actual insertion.
    """
    before = time.time()
    try:
        if not dry_run:
            res = client.insert(collection_name=collection_name, data=batch)
            if "insert_count" not in res or res["insert_count"] != len(batch):
                raise Exception(
                    f"Unexpected number of items inserted: 'insert_count'=={res['insert_count']} with len(batch)=={len(batch)}"
                )
    except Exception as e:
        logger.exception(f"{type(e)} in client.insert({collection_name}, {len(batch)} items)")
        raise
    elapsed = time.time() - before
    if verbose:
        logger.info(f"Inserting {len(batch)} items took {elapsed:.2f}s")


def read_vdb_section(
    fh: IO[str],
    collection_name: str,
    added_documents: dict[str, set[str]],
    client: MilvusClient,
    batch_size: int,
    logger: Any,
    verbose: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Reads and restores a vector database (VDB) section from the backup file.

    Parameters:
        fh:               Backup file handle (already open for reading).
        collection_name:  Target VDB collection name.
        added_documents:  Dict mapping partitions to sets of file IDs that were successfully added in RDB.
        client:           Milvus client instance.
        batch_size:       Number of documents per batch insert.
        logger:           Logger for status and error reporting.
        verbose:          If True, logs additional info.
        dry_run:          If True, no changes are made to the database.
    """
    assert batch_size > 0

    if verbose:
        logger.info("Read vdb section")

    batch = []
    for line in fh:
        # End of section
        if 0 == len(line):
            break

        if len(batch) >= batch_size:
            insert_into_vdb(client, collection_name, batch, logger, verbose, dry_run)
            batch = []

        chunk = json.loads(line)

        if chunk["partition"] in added_documents and chunk["file_id"] in added_documents[chunk["partition"]]:
            chunk.pop("_id", None)
            batch.append(chunk)

    if len(batch) > 0:
        insert_into_vdb(client, collection_name, batch, logger, verbose, dry_run)


def open_backup_file(file_name: str, logger: Any) -> IO[str]:
    """
    Opens a backup file for reading, with support for plain text and LZMA-compressed (.xz) files.

    Parameters:
        file_name:  Path to the backup file.
        logger:     Logger for status and error reporting.

    Returns:
        file object:  Opened file handle in text mode.
    """
    try:
        if file_name.endswith(".xz"):
            import lzma

            return lzma.open(file_name, "rt", encoding="utf-8")
        else:
            return open(file_name, encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed while opening file '{file_name}' for reading:\n" + str(e))
        raise


def main():
    """
    Main entry point:
      - Parses CLI arguments.
      - Loads OpenRAG configuration.
      - Connects to RDB (PostgreSQL) and VDB (Milvus).
      - Restores RDB and VDB data.

    Parameters:
        None (arguments are parsed from sys.argv)

    Returns:
        int: Exit code (0 on success, non-zero on failure).
    """

    def load_openrag_config(logger: Any):
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

    parser = argparse.ArgumentParser(description="OpenRAG restore from backup tool")
    parser.add_argument("-i", "--include-only", nargs="*", help="Include only listed partitions")
    parser.add_argument(
        "-b",
        "--batch-size",
        default=1024,
        type=int,
        help="Batch size used to iterate Milvus",
    )
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Be verbose")
    parser.add_argument(
        "-d",
        "--dry-run",
        default=False,
        action="store_true",
        help="Don't change the target database",
    )
    parser.add_argument("-u", "--user-id", type=int, default=1, help="Create partitions with this user-id")
    parser.add_argument("input", help="input file name")

    args = parser.parse_args()

    logger = get_logger()

    rdb, vdb = load_openrag_config(logger)

    if args.verbose:
        logger.info(f"rdb @ {rdb.host}:{rdb.port} | vdb @ {vdb.host}:{vdb.port} | collection: {vdb.collection_name}")

    database_url = (
        f"postgresql://{rdb.user}:{rdb.password}@{rdb.host}:{rdb.port}/partitions_for_collection_{vdb.collection_name}"
    )

    # List existing partitions
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            existing_partitions = {item["partition"]: item for item in _list_partitions(conn)}
    except Exception as e:
        logger.error(f"Failed while accessing catalog database at {rdb.host}:{rdb.port}\n{e}")
        raise

    if args.include_only:
        for part_name in args.include_only:
            if part_name in existing_partitions:
                logger.error(f'Partition "{part_name}" already exists')
                return 1

    client = MilvusClient(uri=f"http://{vdb.host}:{vdb.port}")

    try:
        with open_backup_file(args.input, logger) as fh, create_engine(database_url).connect() as conn:
            added_documents = {}

            for line in fh:
                line = line.strip()

                if line in ["rdb"]:
                    read_rdb_section(
                        fh,
                        conn,
                        args.include_only,
                        added_documents,
                        existing_partitions,
                        logger,
                        args.user_id,
                        args.verbose,
                        args.dry_run,
                    )

                if line in ["vdb"]:
                    read_vdb_section(
                        fh,
                        vdb.collection_name,
                        added_documents,
                        client,
                        args.batch_size,
                        logger,
                        args.verbose,
                        args.dry_run,
                    )
    except Exception as e:
        logger.error("Error: " + str(e))
        raise
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
