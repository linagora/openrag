"""Builds the ``ChatService``: its capabilities, the ``classic`` graph's steps and the compiled graph.

The graph is compiled here, once, when the container first builds the service,
and runs for every chat request. The capabilities are built once too and
shared: the graph's steps and the completions endpoint read the same
contextualizer, retriever and prompt settings.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from services.capabilities.contextualize import QueryContextualizer
from services.capabilities.retrieve import SourceRetriever
from services.capabilities.scope import ScopeResolver
from services.capabilities.summarize import Summarizer
from services.capabilities.web_search import WebSearch
from services.chat.classic import ClassicSteps
from services.chat.graphs.classic import build_classic_graph
from services.chat.prompt_settings import PartitionPromptSettings
from services.inference.runtime import detect_language, get_llm_semaphore
from services.orchestrators.answer_service import AnswerService
from services.orchestrators.chat_service import ChatService

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
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
    llm_factory: Callable[[str], LLM] | None = None,
) -> ChatService:
    """A ``ChatService`` running the ``classic`` graph over these services.

    *llm* is the static LLM built from ``settings.llm``: the last resort when
    no catalog endpoint answers, and the one map-reduce runs on. *llm_factory*
    resolves the catalog's endpoints by name (a partition's ``chat_llm``, the
    ``"default"`` alias); ``None`` in unit tests. *web_search_service* is
    ``None`` when no provider is configured.
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
    steps = ClassicSteps(
        config=config,
        retrieval_service=retrieval_service,
        prompt_service=prompt_service,
        contextualizer=contextualizer,
        retriever=retriever,
        web_search=web_search,
        summarizer=Summarizer(llm=llm, config=config, semaphore=get_llm_semaphore),
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
        answer_service=AnswerService(detect_language=detect_language),
    )


__all__ = ["create_chat_service"]
