"""Builds the ``ChatService``: its capabilities, the graphs' steps, the agent's tools and the compiled graphs.

The ``classic`` and ``agentic`` graphs are compiled here, once, when the
container first builds the service, and run for every chat request. The
capabilities are built once too and shared: the ``classic`` graph's steps, the
completions endpoint and the agent's tools read the same contextualizer,
retriever, prompt settings and map-reduce.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from di.tools import create_tool_registry
from services.capabilities.contextualize import QueryContextualizer
from services.capabilities.retrieve import SourceRetriever
from services.capabilities.scope import ScopeResolver
from services.capabilities.summarize import Summarizer
from services.capabilities.web_search import WebSearch
from services.chat.agentic import AgenticChat, AgentSteps
from services.chat.classic import ClassicSteps
from services.chat.graphs.agentic import build_agent_graph
from services.chat.graphs.classic import build_classic_graph
from services.chat.prompt_settings import PartitionPromptSettings
from services.inference.runtime import detect_language, get_llm_semaphore
from services.orchestrators.answer_service import AnswerService
from services.orchestrators.chat_service import ChatService

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.ports.document_repo import DocumentRepository
    from core.ports.topic_tag_repo import TopicTagRepository
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.workspace_service import WorkspaceService


def create_chat_service(
    *,
    config: Settings,
    llm: LLM,
    retrieval_service: RetrievalService,
    web_search_service: Any | None,
    workspace_service: WorkspaceService,
    prompt_service: PromptService,
    document_repo: DocumentRepository,
    topic_tag_repo: TopicTagRepository | None = None,
    llm_factory: Callable[[str], LLM] | None = None,
) -> ChatService:
    """A ``ChatService`` running the ``classic`` and ``agentic`` graphs over these services.

    *llm* is the static LLM built from ``settings.llm``: the last resort when
    no catalog endpoint answers, and the one map-reduce runs on. *llm_factory*
    resolves the catalog's endpoints by name (a partition's ``chat_llm``, the
    ``"default"`` alias); ``None`` in unit tests. *web_search_service* is
    ``None`` when no provider is configured. *document_repo* and
    *topic_tag_repo* back the agent's file tools; without tagging, files show
    no topics.
    """
    contextualizer = QueryContextualizer(
        config=config,
        prompt_service=prompt_service,
        llm=llm,
        detect_language=detect_language,
        clock=lambda: datetime.now(UTC),
    )
    web_search = WebSearch(web_search_service)
    retriever = SourceRetriever(retrieval_service=retrieval_service, web_search=web_search)
    prompt_settings = PartitionPromptSettings(config)
    summarizer = Summarizer(llm=llm, config=config, semaphore=get_llm_semaphore)
    steps = ClassicSteps(
        config=config,
        retrieval_service=retrieval_service,
        prompt_service=prompt_service,
        contextualizer=contextualizer,
        retriever=retriever,
        web_search=web_search,
        summarizer=summarizer,
        prompt_settings=prompt_settings,
        clock=datetime.now,
        detect_language=detect_language,
    )
    return ChatService(
        config=config,
        llm=llm,
        llm_factory=llm_factory,
        retrieval_service=retrieval_service,
        prompt_service=prompt_service,
        contextualizer=contextualizer,
        scope_resolver=ScopeResolver(workspace_service),
        retriever=retriever,
        prompt_settings=prompt_settings,
        classic=build_classic_graph(steps),
        agentic=AgenticChat(
            config=config,
            prompt_service=prompt_service,
            registry=create_tool_registry(
                retriever=retriever,
                retrieval_service=retrieval_service,
                prompt_settings=prompt_settings,
                summarizer=summarizer,
                document_repo=document_repo,
                topic_tags=topic_tag_repo,
            ),
            graph=build_agent_graph(AgentSteps()),
            clock=lambda: datetime.now(UTC),
        ),
        answer_service=AnswerService(detect_language=detect_language),
    )


__all__ = ["create_chat_service"]
