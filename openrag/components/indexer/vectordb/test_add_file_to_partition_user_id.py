"""Regression test for #371 — add_file_to_partition must refuse to auto-create
a partition when ``user_id`` is None. PartitionMembership.user_id is
NOT NULL, so a None silently surfaced as IntegrityError at commit time
and left the partition row half-created.
"""

import pytest
from components.indexer.vectordb.models import Base, File, Partition
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


def test_auto_create_refuses_when_user_id_is_none(pfm):
    with pytest.raises(ValueError, match="without a user_id"):
        pfm.add_file_to_partition(file_id="f1", partition="new-part", user_id=None)
    # No partition row should be created
    with pfm.Session() as s:
        assert s.query(Partition).filter_by(partition="new-part").first() is None
        assert s.query(File).filter_by(file_id="f1").first() is None


def test_auto_create_succeeds_with_real_user_id(pfm):
    # Seed a user the bootstrap can be tied to
    from components.indexer.vectordb.models import User

    with pfm.Session() as s:
        s.add(User(id=42, display_name="u42", token="t42", is_admin=False))
        s.commit()

    pfm.add_file_to_partition(file_id="f1", partition="new-part", user_id=42)
    with pfm.Session() as s:
        assert s.query(Partition).filter_by(partition="new-part").first() is not None
        assert s.query(File).filter_by(file_id="f1").first() is not None
