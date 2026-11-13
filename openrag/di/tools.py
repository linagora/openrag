"""Builds the registry of the agent's tools from the services they run on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.tools import ToolRegistry
from services.tools.documents import DocumentTools
from services.tools.files import FileTools

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from core.ports.topic_tag_repo import TopicTagRepository
    from services.capabilities.retrieve import SourceRetriever
    from services.capabilities.summarize import Summarizer
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.retrieval_service import RetrievalService


def create_tool_registry(
    *,
    retriever: SourceRetriever,
    retrieval_service: RetrievalService,
    prompt_settings: PartitionPromptSettings,
    summarizer: Summarizer,
    document_repo: DocumentRepository,
    topic_tags: TopicTagRepository | None = None,
) -> ToolRegistry:
    """Every tool the agent can be offered.

    *retriever*, *prompt_settings* and *summarizer* are the ones the ``classic``
    graph uses, so a tool's search retrieves and cuts, and its map-reduce
    summarizes, exactly as a ``classic`` request does.
    *topic_tags* is ``None`` where tagging isn't wired; files then show none.
    """
    return ToolRegistry.from_toolsets(
        DocumentTools(
            retriever=retriever,
            retrieval_service=retrieval_service,
            prompt_settings=prompt_settings,
            summarizer=summarizer,
        ),
        FileTools(document_repo=document_repo, topic_tags=topic_tags),
    )


__all__ = ["create_tool_registry"]
