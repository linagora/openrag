"""
Unit tests for document relationship functionality.

Tests the relationship_id and parent_id fields for linking related documents
(e.g., email threads, folder hierarchies).

Note: These tests use an in-memory SQLite database to test the PartitionFileManager
methods without requiring the full application stack.
"""

import json

import pytest
from sqlalchemy import Column, DateTime, Index, Integer, String, Text, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func

# Create isolated SQLAlchemy base for testing
TestBase = declarative_base()


class FileModel(TestBase):
    """Test version of File model with relationship fields."""

    __tablename__ = "files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String, nullable=False)
    partition_name = Column(String, nullable=False)
    file_metadata = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    relationship_id = Column(String, nullable=True, index=True)
    parent_id = Column(String, nullable=True, index=True)

    __table_args__ = (
        Index("ix_relationship_partition", "relationship_id", "partition_name"),
        Index("ix_parent_partition", "parent_id", "partition_name"),
    )

    def to_dict(self):
        return {
            "file_id": self.file_id,
            "partition": self.partition_name,
            "file_metadata": (json.loads(self.file_metadata) if self.file_metadata else {}),
            "relationship_id": self.relationship_id,
            "parent_id": self.parent_id,
            "created_at": str(self.created_at) if self.created_at else None,
        }


class PartitionFileManagerHelper:
    """Test version of PartitionFileManager for isolated testing."""

    def __init__(self, session_factory):
        self.Session = session_factory

    def add_file_to_partition(
        self,
        partition: str,
        file_id: str,
        file_metadata: dict = None,
        relationship_id: str = None,
        parent_id: str = None,
    ):
        """Add a file record to the database."""
        with self.Session() as session:
            file_entry = FileModel(
                file_id=file_id,
                partition_name=partition,
                file_metadata=json.dumps(file_metadata) if file_metadata else None,
                relationship_id=relationship_id,
                parent_id=parent_id,
            )
            session.add(file_entry)
            session.commit()

    def get_files_by_relationship(self, partition: str, relationship_id: str) -> list[dict]:
        """Get all files with the same relationship_id in a partition."""
        with self.Session() as session:
            files = (
                session.query(FileModel)
                .filter(
                    FileModel.partition_name == partition,
                    FileModel.relationship_id == relationship_id,
                )
                .all()
            )
            return [f.to_dict() for f in files]

    def get_file_ids_by_relationship(self, partition: str, relationship_id: str) -> list[str]:
        """Get file IDs for files with the same relationship_id."""
        with self.Session() as session:
            files = (
                session.query(FileModel.file_id)
                .filter(
                    FileModel.partition_name == partition,
                    FileModel.relationship_id == relationship_id,
                )
                .all()
            )
            return [f.file_id for f in files]

    def get_file_ancestors(self, partition: str, file_id: str) -> list[dict]:
        """Get the ancestor chain for a file using recursive CTE."""
        with self.Session() as session:
            query = text(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT id, file_id, partition_name, parent_id, file_metadata,
                           relationship_id, 0 as depth
                    FROM files
                    WHERE file_id = :file_id AND partition_name = :partition

                    UNION ALL

                    SELECT f.id, f.file_id, f.partition_name, f.parent_id,
                           f.file_metadata, f.relationship_id, a.depth + 1
                    FROM files f
                    INNER JOIN ancestors a ON f.file_id = a.parent_id
                        AND f.partition_name = a.partition_name
                )
                SELECT * FROM ancestors ORDER BY depth DESC
            """
            )
            result = session.execute(query, {"file_id": file_id, "partition": partition})
            rows = result.fetchall()
            return [
                {
                    "file_id": row.file_id,
                    "partition": row.partition_name,
                    "file_metadata": (json.loads(row.file_metadata) if row.file_metadata else {}),
                    "relationship_id": row.relationship_id,
                    "parent_id": row.parent_id,
                }
                for row in rows
            ]

    def get_ancestor_file_ids(self, partition: str, file_id: str) -> list[str]:
        """Get file IDs of ancestors."""
        ancestors = self.get_file_ancestors(partition, file_id)
        return [a["file_id"] for a in ancestors]


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    TestBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


@pytest.fixture
def file_manager(in_memory_db):
    """Create a PartitionFileManagerHelper with in-memory database."""
    return PartitionFileManagerHelper(in_memory_db)


class TestAddFileWithRelationships:
    """Test adding files with relationship_id and parent_id."""

    def test_add_file_with_relationship_id(self, file_manager):
        """Test that files can be added with a relationship_id."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_001",
            file_metadata={"filename": "email1.eml"},
            relationship_id="thread_abc123",
        )

        with file_manager.Session() as session:
            result = session.execute(text("SELECT relationship_id FROM files WHERE file_id = 'file_001'")).fetchone()
            assert result[0] == "thread_abc123"

    def test_add_file_with_parent_id(self, file_manager):
        """Test that files can be added with a parent_id."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_001",
            file_metadata={"filename": "reply.eml"},
            parent_id="file_000",
        )

        with file_manager.Session() as session:
            result = session.execute(text("SELECT parent_id FROM files WHERE file_id = 'file_001'")).fetchone()
            assert result[0] == "file_000"

    def test_add_file_with_both_relationship_and_parent(self, file_manager):
        """Test that files can be added with both relationship_id and parent_id."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_002",
            file_metadata={"filename": "reply2.eml"},
            relationship_id="thread_abc123",
            parent_id="file_001",
        )

        with file_manager.Session() as session:
            result = session.execute(
                text("SELECT relationship_id, parent_id FROM files WHERE file_id = 'file_002'")
            ).fetchone()
            assert result[0] == "thread_abc123"
            assert result[1] == "file_001"

    def test_add_file_without_relationships(self, file_manager):
        """Test that files can be added without relationship fields (backward compat)."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_003",
            file_metadata={"filename": "standalone.pdf"},
        )

        with file_manager.Session() as session:
            result = session.execute(
                text("SELECT relationship_id, parent_id FROM files WHERE file_id = 'file_003'")
            ).fetchone()
            assert result[0] is None
            assert result[1] is None


class TestGetFilesByRelationship:
    """Test querying files by relationship_id."""

    def test_get_files_by_relationship(self, file_manager):
        """Test retrieving all files with the same relationship_id."""
        # Add multiple files with same relationship_id
        for i in range(3):
            file_manager.add_file_to_partition(
                partition="test_partition",
                file_id=f"email_{i}",
                file_metadata={"filename": f"email{i}.eml"},
                relationship_id="thread_xyz",
            )

        # Add a file with different relationship_id
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="email_other",
            file_metadata={"filename": "other.eml"},
            relationship_id="thread_other",
        )

        results = file_manager.get_files_by_relationship(
            partition="test_partition",
            relationship_id="thread_xyz",
        )

        assert len(results) == 3
        file_ids = [r["file_id"] for r in results]
        assert set(file_ids) == {"email_0", "email_1", "email_2"}

    def test_get_files_by_relationship_empty_result(self, file_manager):
        """Test that empty list is returned when no files match."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_001",
            file_metadata={"filename": "test.pdf"},
            relationship_id="rel_abc",
        )

        results = file_manager.get_files_by_relationship(
            partition="test_partition",
            relationship_id="nonexistent",
        )

        assert results == []

    def test_get_files_by_relationship_respects_partition(self, file_manager):
        """Test that relationship query respects partition boundaries."""
        # Add files with same relationship_id in different partitions
        file_manager.add_file_to_partition(
            partition="partition_a",
            file_id="file_a",
            file_metadata={"filename": "a.pdf"},
            relationship_id="shared_rel",
        )
        file_manager.add_file_to_partition(
            partition="partition_b",
            file_id="file_b",
            file_metadata={"filename": "b.pdf"},
            relationship_id="shared_rel",
        )

        results = file_manager.get_files_by_relationship(
            partition="partition_a",
            relationship_id="shared_rel",
        )

        assert len(results) == 1
        assert results[0]["file_id"] == "file_a"

    def test_get_file_ids_by_relationship(self, file_manager):
        """Test retrieving only file IDs by relationship_id."""
        for i in range(3):
            file_manager.add_file_to_partition(
                partition="test_partition",
                file_id=f"doc_{i}",
                file_metadata={"filename": f"doc{i}.pdf"},
                relationship_id="folder_123",
            )

        file_ids = file_manager.get_file_ids_by_relationship(
            partition="test_partition",
            relationship_id="folder_123",
        )

        assert len(file_ids) == 3
        assert set(file_ids) == {"doc_0", "doc_1", "doc_2"}


class TestGetFileAncestors:
    """Test retrieving ancestor chain for a file."""

    def test_get_file_ancestors_single_file(self, file_manager):
        """Test that a file with no parent returns only itself."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="root_email",
            file_metadata={"filename": "root.eml"},
        )

        ancestors = file_manager.get_file_ancestors(
            partition="test_partition",
            file_id="root_email",
        )

        assert len(ancestors) == 1
        assert ancestors[0]["file_id"] == "root_email"

    def test_get_file_ancestors_chain(self, file_manager):
        """Test retrieving a chain of ancestors."""
        # Create email thread: root -> reply1 -> reply2
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="email_root",
            file_metadata={"filename": "root.eml"},
            relationship_id="thread_1",
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="email_reply1",
            file_metadata={"filename": "reply1.eml"},
            relationship_id="thread_1",
            parent_id="email_root",
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="email_reply2",
            file_metadata={"filename": "reply2.eml"},
            relationship_id="thread_1",
            parent_id="email_reply1",
        )

        ancestors = file_manager.get_file_ancestors(
            partition="test_partition",
            file_id="email_reply2",
        )

        # Should return [root, reply1, reply2] in order from root to target
        assert len(ancestors) == 3
        file_ids = [a["file_id"] for a in ancestors]
        assert file_ids == ["email_root", "email_reply1", "email_reply2"]

    def test_get_file_ancestors_returns_ordered_path(self, file_manager):
        """Test that ancestors are returned in correct order (root first)."""
        # Create deeper hierarchy: A -> B -> C -> D
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_a",
            file_metadata={"filename": "a.txt"},
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_b",
            file_metadata={"filename": "b.txt"},
            parent_id="file_a",
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_c",
            file_metadata={"filename": "c.txt"},
            parent_id="file_b",
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_d",
            file_metadata={"filename": "d.txt"},
            parent_id="file_c",
        )

        ancestors = file_manager.get_file_ancestors(
            partition="test_partition",
            file_id="file_d",
        )

        # Verify order: root first, target last
        assert len(ancestors) == 4
        file_ids = [a["file_id"] for a in ancestors]
        assert file_ids == ["file_a", "file_b", "file_c", "file_d"]

    def test_get_file_ancestors_nonexistent_file(self, file_manager):
        """Test that empty list is returned for nonexistent file."""
        ancestors = file_manager.get_file_ancestors(
            partition="test_partition",
            file_id="nonexistent",
        )

        assert ancestors == []

    def test_get_ancestor_file_ids(self, file_manager):
        """Test retrieving only ancestor file IDs."""
        # Create chain: root -> child
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="parent_file",
            file_metadata={"filename": "parent.txt"},
        )
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="child_file",
            file_metadata={"filename": "child.txt"},
            parent_id="parent_file",
        )

        ancestor_ids = file_manager.get_ancestor_file_ids(
            partition="test_partition",
            file_id="child_file",
        )

        assert len(ancestor_ids) == 2
        assert ancestor_ids == ["parent_file", "child_file"]


class TestFileModelFields:
    """Test that File model correctly handles relationship fields."""

    def test_to_dict_includes_relationship_fields(self, file_manager):
        """Test that to_dict() includes relationship_id and parent_id."""
        file_manager.add_file_to_partition(
            partition="test_partition",
            file_id="file_001",
            file_metadata={"filename": "test.eml", "subject": "Hello"},
            relationship_id="thread_abc",
            parent_id="file_000",
        )

        files = file_manager.get_files_by_relationship(
            partition="test_partition",
            relationship_id="thread_abc",
        )

        assert len(files) == 1
        file_dict = files[0]
        assert file_dict["relationship_id"] == "thread_abc"
        assert file_dict["parent_id"] == "file_000"
        assert file_dict["file_metadata"]["filename"] == "test.eml"
        assert file_dict["file_metadata"]["subject"] == "Hello"
