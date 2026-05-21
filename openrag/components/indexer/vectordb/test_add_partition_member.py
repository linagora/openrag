"""Regression test for #387 — auto-creating a partition via add_partition_member
must require role='owner'. Anything else would yield an ownerless partition,
which permanently locks non-admins out of owner-only endpoints.
"""

import pytest
from components.indexer.vectordb.models import Base, Partition, PartitionMembership, User
from components.indexer.vectordb.utils import PartitionFileManager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from utils.logger import get_logger


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


def _insert_user(pfm, uid: int) -> None:
    with pfm.Session() as s:
        s.add(User(id=uid, display_name=f"u{uid}", token=f"t{uid}", is_admin=False))
        s.commit()


def test_add_member_with_editor_role_refuses_to_create_partition(pfm):
    _insert_user(pfm, 5)
    with pytest.raises(ValueError, match="first member must have role='owner'"):
        pfm.add_partition_member("brand-new", 5, "editor")
    with pfm.Session() as s:
        assert s.query(Partition).filter_by(partition="brand-new").first() is None


def test_add_member_with_viewer_role_refuses_to_create_partition(pfm):
    _insert_user(pfm, 5)
    with pytest.raises(ValueError, match="role='owner'"):
        pfm.add_partition_member("brand-new", 5, "viewer")


def test_add_member_with_owner_role_creates_partition(pfm):
    _insert_user(pfm, 5)
    assert pfm.add_partition_member("brand-new", 5, "owner") is True
    with pfm.Session() as s:
        assert s.query(Partition).filter_by(partition="brand-new").first() is not None
        m = s.query(PartitionMembership).filter_by(partition_name="brand-new", user_id=5).first()
        assert m is not None
        assert m.role == "owner"


def test_add_member_to_existing_partition_allows_any_role(pfm):
    """Once the partition exists, any role is accepted (the guard is only about
    the auto-create path)."""
    _insert_user(pfm, 5)
    pfm.add_partition_member("existing", 5, "owner")
    _insert_user(pfm, 6)
    # adding a non-owner role to an existing partition is fine
    assert pfm.add_partition_member("existing", 6, "editor") is True
    with pfm.Session() as s:
        m = s.query(PartitionMembership).filter_by(partition_name="existing", user_id=6).first()
        assert m.role == "editor"
