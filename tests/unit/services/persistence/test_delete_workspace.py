"""Unit tests for delete_workspace orphan detection.

Verifies that delete_workspace() only returns file IDs that are truly
orphaned (not in any other workspace AND not independently indexed in the
files table), preventing accidental data loss.

Uses an in-memory SQLite database — no Ray, no real Postgres required.
"""

import pytest
from sqlalchemy import Column, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

TestBase = declarative_base()


class FileModel(TestBase):
    """Minimal files table."""

    __tablename__ = "files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String, nullable=False)
    partition_name = Column(String, nullable=False)

    __table_args__ = (UniqueConstraint("file_id", "partition_name", name="uix_file_id_partition"),)


class WorkspaceModel(TestBase):
    """Minimal workspaces table."""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(String, nullable=False, unique=True)
    partition_name = Column(String, nullable=False)


class WorkspaceFileModel(TestBase):
    """Minimal workspace_files table."""

    __tablename__ = "workspace_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(String, nullable=False)
    file_id = Column(String, nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id", "file_id", name="uix_workspace_file"),)


class DeleteWorkspaceHelper:
    """Reproduces the delete_workspace orphan logic for isolated testing."""

    def __init__(self, session_factory):
        self.Session = session_factory

    def add_file(self, partition: str, file_id: str):
        with self.Session() as s:
            s.add(FileModel(file_id=file_id, partition_name=partition))
            s.commit()

    def add_workspace(self, workspace_id: str, partition: str):
        with self.Session() as s:
            s.add(WorkspaceModel(workspace_id=workspace_id, partition_name=partition))
            s.commit()

    def add_file_to_workspace(self, workspace_id: str, file_id: str):
        with self.Session() as s:
            s.add(WorkspaceFileModel(workspace_id=workspace_id, file_id=file_id))
            s.commit()

    def delete_workspace(self, workspace_id: str) -> list[str]:
        """Mirror of PartitionFileManager.delete_workspace — returns orphaned file_ids."""
        from sqlalchemy import delete, select

        with self.Session() as session:
            workspace = session.execute(
                select(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id)
            ).scalar_one_or_none()
            if workspace is None:
                return []
            partition = workspace.partition_name

            subq_other_ws = select(WorkspaceFileModel.file_id).where(WorkspaceFileModel.workspace_id != workspace_id)
            # Scoped to the workspace's partition so a same-named file in another
            # partition does not incorrectly prevent orphan detection here.
            subq_indexed = select(FileModel.file_id).where(FileModel.partition_name == partition)
            result = session.execute(
                select(WorkspaceFileModel.file_id)
                .where(WorkspaceFileModel.workspace_id == workspace_id)
                .where(WorkspaceFileModel.file_id.notin_(subq_other_ws))
                .where(WorkspaceFileModel.file_id.notin_(subq_indexed))
            )
            orphaned = [r[0] for r in result.all()]
            session.execute(delete(WorkspaceFileModel).where(WorkspaceFileModel.workspace_id == workspace_id))
            session.execute(delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id))
            session.commit()
            return orphaned


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    TestBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    helper = DeleteWorkspaceHelper(Session)
    yield helper
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_independently_indexed_file_is_not_orphaned(db):
    """A file present in the files table must never be returned as orphaned."""
    db.add_file("p1", "file-a")
    db.add_workspace("ws1", "p1")
    db.add_file_to_workspace("ws1", "file-a")

    orphans = db.delete_workspace("ws1")

    assert orphans == []


def test_file_only_in_workspace_is_orphaned(db):
    """A file that exists only as a workspace entry (not in files table) is orphaned."""
    db.add_workspace("ws1", "p1")
    db.add_file_to_workspace("ws1", "ghost-file")

    orphans = db.delete_workspace("ws1")

    assert orphans == ["ghost-file"]


def test_file_in_another_workspace_is_not_orphaned(db):
    """A file shared between two workspaces is not orphaned when one workspace is deleted."""
    db.add_workspace("ws1", "p1")
    db.add_workspace("ws2", "p1")
    db.add_file_to_workspace("ws1", "shared-file")
    db.add_file_to_workspace("ws2", "shared-file")

    orphans = db.delete_workspace("ws1")

    assert orphans == []


def test_mixed_files_only_ghost_is_orphaned(db):
    """Only truly orphaned (not indexed, not in other workspace) files are returned."""
    db.add_file("p1", "indexed-file")
    db.add_workspace("ws1", "p1")
    db.add_workspace("ws2", "p1")
    db.add_file_to_workspace("ws1", "indexed-file")  # also in files table
    db.add_file_to_workspace("ws1", "shared-file")  # also in ws2
    db.add_file_to_workspace("ws2", "shared-file")
    db.add_file_to_workspace("ws1", "ghost-file")  # only in ws1, not in files

    orphans = db.delete_workspace("ws1")

    assert orphans == ["ghost-file"]


def test_empty_workspace_returns_no_orphans(db):
    db.add_workspace("ws1", "p1")

    orphans = db.delete_workspace("ws1")

    assert orphans == []


def test_file_indexed_in_different_partition_is_still_orphaned(db):
    """A file_id that exists in the files table under a *different* partition must
    not prevent orphan detection in the workspace's own partition."""
    db.add_file("p2", "doc.pdf")  # indexed in p2, NOT in p1
    db.add_workspace("ws1", "p1")
    db.add_file_to_workspace("ws1", "doc.pdf")  # workspace lives in p1

    orphans = db.delete_workspace("ws1")

    # doc.pdf is not indexed in p1, so it is a true orphan of ws1
    assert orphans == ["doc.pdf"]


def test_nonexistent_workspace_returns_empty_list(db):
    """Deleting a workspace that doesn't exist returns an empty list."""
    orphans = db.delete_workspace("nonexistent-ws")
    assert orphans == []
