"""Regression test for #369 — get_file_ancestors must apply a hard depth
cap regardless of the caller-supplied value, to defend against cyclic
parent_id chains that would otherwise loop until the DB aborts.
"""

import pytest
from components.indexer.vectordb.models import Base, Partition
from components.indexer.vectordb.utils import PartitionFileManager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from utils.logger import get_logger


def _insert_cyclic_chain(pfm, partition: str):
    """Insert two files whose parent_id pointers loop back to each other.

    ``a.parent_id == "b"`` and ``b.parent_id == "a"`` — traversing this with
    no depth guard would recurse forever. We write the rows with raw SQL so
    ``file_metadata`` stays SQL ``NULL`` (the ORM column defaults to ``{}``,
    which SQLite would surface to the raw CTE query as the string ``"{}"``
    and break the row-flattening ``**(row.file_metadata or {})``).
    """
    with pfm.Session() as s:
        s.add(Partition(partition=partition))
        s.commit()
        s.execute(
            text(
                "INSERT INTO files "
                "(file_id, partition_name, relationship_id, parent_id, file_metadata) "
                "VALUES (:fid, :p, :rid, :pid, NULL)"
            ),
            [
                {"fid": "a", "p": partition, "rid": "rel", "pid": "b"},
                {"fid": "b", "p": partition, "rid": "rel", "pid": "a"},
            ],
        )
        s.commit()


@pytest.fixture()
def pfm():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    mgr = PartitionFileManager.__new__(PartitionFileManager)
    mgr.engine = engine
    mgr.Session = Session
    mgr.logger = get_logger()
    mgr.file_quota_per_user = -1
    yield mgr
    engine.dispose()


def test_hard_cap_lives_in_retriever_config():
    """The cap value must come from the retriever config (operator-tunable),
    not be hardcoded inside the data-access layer.

    We assert the contract — the cap is a positive integer — rather than a
    specific magnitude, so legitimate operator configurations don't fail.
    """
    from config import load_config

    cap = load_config().retriever.max_ancestor_depth_cap
    assert isinstance(cap, int)
    assert cap > 0


def test_none_max_depth_is_clamped(pfm):
    """When max_ancestor_depth=None, the query must run with the hard cap
    instead of unlimited recursion. We only verify that the call returns
    a list (which it would not if the recursion were unbounded against a
    cyclic chain). For this smoke test the partition is empty so the
    result is []."""
    with pfm.Session() as s:
        s.add(Partition(partition="p"))
        s.commit()
    out = pfm.get_file_ancestors(partition="p", file_id="missing", max_ancestor_depth=None)
    assert out == []


def test_explicit_depth_above_cap_is_clamped(pfm):
    """An explicit ``max_ancestor_depth`` larger than the hard cap must
    be silently clamped — a misconfigured caller cannot bypass the
    safety net.
    """
    from config import load_config

    cap = int(load_config().retriever.max_ancestor_depth_cap)
    with pfm.Session() as s:
        s.add(Partition(partition="p"))
        s.commit()
    # Smoke test: large value still returns cleanly (it would hang under
    # the pre-fix code on a cyclic chain).
    out = pfm.get_file_ancestors(partition="p", file_id="missing", max_ancestor_depth=cap * 10)
    assert out == []


def test_cyclic_chain_is_bounded_by_explicit_depth(pfm):
    """An actual a→b→a cycle must terminate at the (clamped) depth instead
    of recursing forever. With an explicit depth below the hard cap, the
    walk stops at exactly that many levels."""
    _insert_cyclic_chain(pfm, partition="p")
    depth = 5
    out = pfm.get_file_ancestors(partition="p", file_id="a", max_ancestor_depth=depth)
    # Base row (depth 0) plus one row per recursion step while depth < cap.
    assert len(out) == depth + 1
    assert max(row["depth"] for row in out) == depth


def test_cyclic_chain_is_bounded_by_hard_cap(pfm):
    """With ``max_ancestor_depth=None`` a cyclic chain must still terminate,
    bounded by the configured hard cap rather than looping indefinitely."""
    from config import load_config

    cap = int(load_config().retriever.max_ancestor_depth_cap)
    _insert_cyclic_chain(pfm, partition="p")
    out = pfm.get_file_ancestors(partition="p", file_id="a", max_ancestor_depth=None)
    assert len(out) == cap + 1


# NB: these tests run against SQLite, which supports WITH RECURSIVE. The rows
# above carry NULL file_metadata on purpose — a non-null JSON value would be
# surfaced by SQLite to the raw-SQL CTE as a str and break row flattening, so
# exercising the *metadata* unpacking path still requires Postgres.
