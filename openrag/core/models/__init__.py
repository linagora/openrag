"""Domain models — pure Pydantic, no infrastructure imports."""

from .catalog import DocumentRecord, DocumentStatus, IndexationJob, JobStatus
from .chunk import Chunk, ChunkType
from .contextualization import ContextualizedQuery
from .conversation import Conversation, Message
from .document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from .prompt import Prompt, PromptType
from .query import Query, RetrievalQuery, SearchQueries, TemporalPredicate
from .retrieval_response import RetrievalResponse
from .retrieval_result import RetrievalResult, ScoredChunk
from .user import ApiKey, OIDCSession, PartitionRole, TokenPayload, User, UserPartition
from .workspace import Workspace

__all__ = [
    "ApiKey",
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
    "Query",
    "RetrievalQuery",
    "RetrievalResponse",
    "RetrievalResult",
    "ScoredChunk",
    "SearchQueries",
    "TemporalPredicate",
    "TextBlock",
    "TokenPayload",
    "User",
    "UserPartition",
    "Workspace",
]
