"""Regression test for #361 — _ensure_admin_user must not rotate the admin
token on every startup when AUTH_TOKEN is unset.

Previously the bootstrap generated a fresh ``or-`` token whenever
``AUTH_TOKEN`` was unset and unconditionally overwrote the existing row.
That invalidated any token issued via ``POST /users/1/regenerate_token``
on every container restart, with no audit trail.
"""

import pytest
from components.indexer.vectordb.models import Base, User
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


def test_first_call_creates_admin_with_provided_token(pfm):
    pfm._ensure_admin_user("or-static-token")
    with pfm.Session() as s:
        admin = s.query(User).filter_by(id=1).first()
        assert admin is not None
        assert admin.is_admin is True
        assert admin.token == pfm.hash_token("or-static-token")


def test_restart_with_no_auth_token_preserves_existing_token(pfm):
    # Bootstrap with a fresh row + explicit token
    pfm._ensure_admin_user("or-original")
    with pfm.Session() as s:
        orig_hash = s.query(User).filter_by(id=1).first().token

    # Simulate a /users/1/regenerate_token rotation
    new_hash = pfm.hash_token("or-rotated")
    with pfm.Session() as s:
        admin = s.query(User).filter_by(id=1).first()
        admin.token = new_hash
        s.commit()

    # "Restart" with AUTH_TOKEN unset — must NOT overwrite the rotated token
    pfm._ensure_admin_user("")
    with pfm.Session() as s:
        kept = s.query(User).filter_by(id=1).first().token
    assert kept == new_hash, "rotated admin token was clobbered on restart"
    assert kept != orig_hash


def test_restart_with_auth_token_syncs_db_to_env(pfm):
    pfm._ensure_admin_user("or-old")
    pfm._ensure_admin_user("or-new")
    with pfm.Session() as s:
        admin = s.query(User).filter_by(id=1).first()
    assert admin.token == pfm.hash_token("or-new")
