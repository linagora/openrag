#!/usr/bin/env python3
"""Check that each user's materialized file_count matches the actual count in the files table."""

import argparse
import os
import sys

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

# Add parent dirs so we can import the models
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from components.indexer.vectordb.utils import File, User


def build_database_url(args):
    host = args.host or os.environ.get("POSTGRES_HOST", "localhost")
    port = args.port or os.environ.get("POSTGRES_PORT", "5432")
    user = args.user or os.environ.get("POSTGRES_USER", "openrag")
    password = args.password or os.environ.get("POSTGRES_PASSWORD", "openrag_password")
    collection = args.collection or os.environ.get("VDB_COLLECTION_NAME", "vdb_test")
    db_name = f"partitions_for_collection_{collection}"
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def check_file_counts(database_url, fix=False):
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Actual file counts per user from the files table
        actual_counts = dict(
            session.query(File.created_by, func.count(File.id))
            .filter(File.created_by.isnot(None))
            .group_by(File.created_by)
            .all()
        )

        users = session.query(User).order_by(User.id).all()

        rows = []
        has_mismatch = False
        for u in users:
            actual = actual_counts.get(u.id, 0)
            ok = u.file_count == actual
            if not ok:
                has_mismatch = True
            rows.append((u.id, u.display_name or "", u.file_count, actual, ok))

        # Print table
        green = "\033[92m"
        red = "\033[91m"
        bold = "\033[1m"
        reset = "\033[0m"

        header = f"{'ID':>4}  {'Display Name':<30}  {'Stored':>8}  {'Actual':>8}  {'Status'}"
        print(f"\n{bold}{header}{reset}")
        print("-" * len(header.expandtabs()))

        for uid, name, stored, actual, ok in rows:
            if ok:
                status = f"{green}OK{reset}"
            else:
                status = f"{red}MISMATCH{reset}"
            print(f"{uid:>4}  {name:<30}  {stored:>8}  {actual:>8}  {status}")

        print()

        if not has_mismatch:
            print(f"{green}All file counts are correct.{reset}")
        else:
            print(f"{red}Mismatches detected!{reset}")
            if fix:
                for uid, _, stored, actual, ok in rows:
                    if not ok:
                        session.query(User).filter(User.id == uid).update({User.file_count: actual})
                        print(f"  Fixed user {uid}: {stored} -> {actual}")
                session.commit()
                print(f"{green}All counts have been fixed.{reset}")
            else:
                print(f"Run with {bold}--fix{reset} to correct the values.")

        return 0 if not has_mismatch else 1


def main():
    parser = argparse.ArgumentParser(description="Verify user file_count against actual files in the database.")
    parser.add_argument("--host", help="PostgreSQL host (default: $POSTGRES_HOST or localhost)")
    parser.add_argument("--port", help="PostgreSQL port (default: $POSTGRES_PORT or 5432)")
    parser.add_argument("--user", help="PostgreSQL user (default: $POSTGRES_USER or openrag)")
    parser.add_argument("--password", help="PostgreSQL password (default: $POSTGRES_PASSWORD)")
    parser.add_argument("--collection", help="VDB collection name (default: $VDB_COLLECTION_NAME or vdb_test)")
    parser.add_argument("--fix", action="store_true", help="Fix mismatched counts in the database")
    args = parser.parse_args()

    database_url = build_database_url(args)
    sys.exit(check_file_counts(database_url, fix=args.fix))


if __name__ == "__main__":
    main()
