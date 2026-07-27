"""Domain models — plain Pydantic models or dataclasses, no infrastructure imports.

Types that are validated at a boundary (parsed input, stored rows) are Pydantic;
purely internal value objects may be dataclasses. Either way nothing here may
import from ``services`` or ``api``.
"""

from .catalog import TERMINAL_TASK_STATES, DocumentRecord, DocumentStatus, IndexationJob, JobStatus
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
    "TERMINAL_TASK_STATES",
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
