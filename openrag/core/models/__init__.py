"""Domain models — pure Pydantic, no infrastructure imports."""

from openrag.core.models.catalog import DocumentRecord, DocumentStatus, IndexationJob, JobStatus
from openrag.core.models.chunk import Chunk, ChunkType
from openrag.core.models.contextualization import ContextualizedQuery
from openrag.core.models.conversation import Conversation, Message
from openrag.core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from openrag.core.models.prompt import Prompt, PromptType
from openrag.core.models.query import RetrievalQuery
from openrag.core.models.retrieval_response import RetrievalResponse
from openrag.core.models.retrieval_result import RetrievalResult, ScoredChunk
from openrag.core.models.user import OIDCSession, PartitionRole, User, UserPartition

__all__ = [
    "Chunk",
    "ChunkType",
    "ContextualizedQuery",
    "Conversation",
    "Document",
    "DocumentRecord",
    "DocumentStatus",
    "DocumentType",
    "ImageBlock",
    "IndexationJob",
    "JobStatus",
    "Message",
    "OIDCSession",
    "PartitionRole",
    "ProcessedDocument",
    "Prompt",
    "PromptType",
    "RetrievalQuery",
    "RetrievalResponse",
    "RetrievalResult",
    "ScoredChunk",
    "TextBlock",
    "User",
    "UserPartition",
]
