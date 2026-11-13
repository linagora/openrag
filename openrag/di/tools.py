"""Builds the registry of the agent's tools from the services they run on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.tools import ToolRegistry
from services.tools.documents import DocumentTools

if TYPE_CHECKING:
    from services.capabilities.retrieve import SourceRetriever
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.retrieval_service import RetrievalService


def create_tool_registry(
    *,
    retriever: SourceRetriever,
    retrieval_service: RetrievalService,
    prompt_settings: PartitionPromptSettings,
) -> ToolRegistry:
    """Every tool the agent can be offered.

    *retriever* and *prompt_settings* are the ones the ``classic`` graph uses,
    so a tool's search retrieves and cuts exactly as a ``classic`` request does.
    """
    return ToolRegistry.from_toolsets(
        DocumentTools(retriever=retriever, retrieval_service=retrieval_service, prompt_settings=prompt_settings),
    )


__all__ = ["create_tool_registry"]
