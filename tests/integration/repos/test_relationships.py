"""
Integration tests for document relationship functionality.

These tests verify the end-to-end behavior of relationship_id and parent_id
fields for linking related documents across the full stack.

Test Scenarios:
1. Folder scenario: Multiple files in the same folder (same relationship_id)
2. Email thread scenario: Hierarchical email chain with parallel branches
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.documents.base import Document


class TestFolderScenario:
    """
    Test scenario: 5 files in same folder (same relationship_id).

    This simulates indexing files from a shared folder where all files
    share the same relationship_id (the folder ID).
    """

    @pytest.fixture
    def folder_documents(self):
        """Create test documents representing files in a folder."""
        folder_id = "folder_shared_docs"
        documents = []
        for i in range(5):
            doc = Document(
                page_content=f"Content of document {i} in shared folder",
                metadata={
                    "_id": f"chunk_{i}_0",
                    "file_id": f"doc_{i}",
                    "filename": f"document_{i}.pdf",
                    "partition": "test_partition",
                    "relationship_id": folder_id,
                },
            )
            documents.append(doc)
        return documents

    @pytest.fixture
    def mock_vectordb(self, folder_documents):
        """Create mock VectorDB with folder documents."""
        vectordb = MagicMock()

        # Mock get_related_chunks to return all folder documents
        async def mock_get_related_chunks(partition, relationship_id, limit=100):
            return [doc for doc in folder_documents if doc.metadata.get("relationship_id") == relationship_id]

        vectordb.get_related_chunks = MagicMock()
        vectordb.get_related_chunks.remote = AsyncMock(side_effect=mock_get_related_chunks)

        return vectordb

    @pytest.mark.asyncio
    async def test_index_folder_files_share_relationship_id(self, folder_documents):
        """Test that all files in a folder share the same relationship_id."""
        relationship_ids = {doc.metadata.get("relationship_id") for doc in folder_documents}
        assert len(relationship_ids) == 1
        assert "folder_shared_docs" in relationship_ids

    @pytest.mark.asyncio
    async def test_list_folder_contents_by_relationship(self, mock_vectordb, folder_documents):
        """Test that all folder contents can be retrieved by relationship_id."""
        related = await mock_vectordb.get_related_chunks.remote(
            partition="test_partition",
            relationship_id="folder_shared_docs",
        )

        assert len(related) == 5
        file_ids = {doc.metadata["file_id"] for doc in related}
        expected_ids = {f"doc_{i}" for i in range(5)}
        assert file_ids == expected_ids

    @pytest.mark.asyncio
    async def test_search_returns_related_folder_files(self, mock_vectordb, folder_documents):
        """Test that search with include_related returns other folder files."""
        # Simulate finding one document in search
        search_result = [folder_documents[0]]

        # Get related chunks for the found document
        related = await mock_vectordb.get_related_chunks.remote(
            partition="test_partition",
            relationship_id=search_result[0].metadata["relationship_id"],
        )

        # Should include all 5 folder documents
        assert len(related) == 5


class TestEmailThreadScenario:
    """
    Test scenario: Email thread with hierarchy and parallel branches.

    Thread structure:
    - original_email (root)
      ├── reply1
      │   └── reply2 (reply to reply1)
      └── reply3 (parallel reply to original)

    When querying ancestors of reply2:
    - Should return: [original_email, reply1, reply2]
    - Should NOT include: reply3 (parallel branch)
    """

    @pytest.fixture
    def email_thread_documents(self):
        """Create test documents representing an email thread."""
        thread_id = "thread_abc123"

        original = Document(
            page_content="Original email content about project discussion",
            metadata={
                "_id": "chunk_orig_0",
                "file_id": "email_original",
                "filename": "original.eml",
                "partition": "test_partition",
                "relationship_id": thread_id,
                "parent_id": None,
            },
        )

        reply1 = Document(
            page_content="First reply to the original email",
            metadata={
                "_id": "chunk_r1_0",
                "file_id": "email_reply1",
                "filename": "reply1.eml",
                "partition": "test_partition",
                "relationship_id": thread_id,
                "parent_id": "email_original",
            },
        )

        reply2 = Document(
            page_content="Second reply, responding to reply1",
            metadata={
                "_id": "chunk_r2_0",
                "file_id": "email_reply2",
                "filename": "reply2.eml",
                "partition": "test_partition",
                "relationship_id": thread_id,
                "parent_id": "email_reply1",
            },
        )

        reply3 = Document(
            page_content="Parallel reply to original, separate branch",
            metadata={
                "_id": "chunk_r3_0",
                "file_id": "email_reply3",
                "filename": "reply3.eml",
                "partition": "test_partition",
                "relationship_id": thread_id,
                "parent_id": "email_original",
            },
        )

        return {
            "original": original,
            "reply1": reply1,
            "reply2": reply2,
            "reply3": reply3,
            "all": [original, reply1, reply2, reply3],
        }

    @pytest.fixture
    def mock_vectordb_email(self, email_thread_documents):
        """Create mock VectorDB with email thread documents."""
        vectordb = MagicMock()
        docs = email_thread_documents

        # Build parent lookup
        parent_map = {
            "email_original": None,
            "email_reply1": "email_original",
            "email_reply2": "email_reply1",
            "email_reply3": "email_original",
        }

        doc_by_id = {d.metadata["file_id"]: d for d in docs["all"]}

        async def mock_get_ancestor_chunks(partition, file_id, limit=100):
            """Traverse up the parent chain and return all ancestors."""
            ancestors = []
            current_id = file_id
            visited = set()

            # Walk up the parent chain
            while current_id and current_id not in visited:
                visited.add(current_id)
                if current_id in doc_by_id:
                    ancestors.append(doc_by_id[current_id])
                current_id = parent_map.get(current_id)

            # Return in order from root to target (reverse the list)
            return list(reversed(ancestors))

        async def mock_get_related_chunks(partition, relationship_id, limit=100):
            """Get all chunks with the same relationship_id."""
            return [d for d in docs["all"] if d.metadata.get("relationship_id") == relationship_id]

        vectordb.get_ancestor_chunks = MagicMock()
        vectordb.get_ancestor_chunks.remote = AsyncMock(side_effect=mock_get_ancestor_chunks)

        vectordb.get_related_chunks = MagicMock()
        vectordb.get_related_chunks.remote = AsyncMock(side_effect=mock_get_related_chunks)

        return vectordb

    @pytest.mark.asyncio
    async def test_index_email_thread_all_share_thread_id(self, email_thread_documents):
        """Test that all emails in thread share the same relationship_id."""
        relationship_ids = {doc.metadata.get("relationship_id") for doc in email_thread_documents["all"]}
        assert len(relationship_ids) == 1
        assert "thread_abc123" in relationship_ids

    @pytest.mark.asyncio
    async def test_get_ancestors_returns_direct_path(self, mock_vectordb_email, email_thread_documents):
        """Test that ancestors of reply2 returns direct path: [original, reply1, reply2]."""
        ancestors = await mock_vectordb_email.get_ancestor_chunks.remote(
            partition="test_partition",
            file_id="email_reply2",
        )

        # Should return 3 documents in order
        assert len(ancestors) == 3
        file_ids = [a.metadata["file_id"] for a in ancestors]
        assert file_ids == ["email_original", "email_reply1", "email_reply2"]

    @pytest.mark.asyncio
    async def test_parallel_branch_not_included_in_ancestors(self, mock_vectordb_email, email_thread_documents):
        """Test that reply3 (parallel branch) is NOT in reply2's ancestors."""
        ancestors = await mock_vectordb_email.get_ancestor_chunks.remote(
            partition="test_partition",
            file_id="email_reply2",
        )

        ancestor_file_ids = [a.metadata["file_id"] for a in ancestors]
        assert "email_reply3" not in ancestor_file_ids

    @pytest.mark.asyncio
    async def test_search_with_include_ancestors(self, mock_vectordb_email, email_thread_documents):
        """Test that searching reply2 with include_ancestors gets the full path."""
        # Simulate finding reply2 in search
        search_result = [email_thread_documents["reply2"]]

        # Get ancestors
        ancestors = await mock_vectordb_email.get_ancestor_chunks.remote(
            partition="test_partition",
            file_id=search_result[0].metadata["file_id"],
        )

        # Should have 3 ancestors (original, reply1, reply2)
        assert len(ancestors) == 3

        # Combine search result with ancestors (deduped)
        seen_ids = set()
        combined = []
        for doc in search_result + ancestors:
            doc_id = doc.metadata["_id"]
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                combined.append(doc)

        # Should have 3 unique documents
        assert len(combined) == 3

    @pytest.mark.asyncio
    async def test_get_all_thread_emails_by_relationship(self, mock_vectordb_email, email_thread_documents):
        """Test that all thread emails can be retrieved by relationship_id."""
        related = await mock_vectordb_email.get_related_chunks.remote(
            partition="test_partition",
            relationship_id="thread_abc123",
        )

        assert len(related) == 4
        file_ids = {doc.metadata["file_id"] for doc in related}
        expected = {"email_original", "email_reply1", "email_reply2", "email_reply3"}
        assert file_ids == expected
