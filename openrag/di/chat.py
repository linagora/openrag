"""Builds the ``ChatService`` and the steps it finds a request's sources with.

The capabilities are built once, when the container first builds the service,
and shared by chat and completions.
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
    """A ``ChatService`` over these services.

    *llm* is the static LLM built from ``settings.llm``: the last resort when
    no catalog endpoint answers, and the one map-reduce runs on. *llm_factory*
    resolves the catalog's endpoints by name (a partition's ``chat_llm``, the
    ``"default"`` alias); ``None`` in unit tests. *web_search_service* is
    ``None`` when no provider is configured.
    """
    web_search = WebSearch(web_search_service)
    return ChatService(
        config=config,
        llm=llm,
        llm_factory=llm_factory,
        retrieval_service=retrieval_service,
        prompt_service=prompt_service,
        contextualizer=QueryContextualizer(
            config=config,
            prompt_service=prompt_service,
            llm=llm,
            detect_language=detect_language,
            clock=lambda: datetime.now(UTC),
        ),
        scope_resolver=ScopeResolver(workspace_service),
        web_search=web_search,
        retriever=SourceRetriever(retrieval_service=retrieval_service, web_search=web_search),
        summarizer=Summarizer(llm=llm, config=config, semaphore=get_llm_semaphore),
        answer_service=AnswerService(detect_language=detect_language),
    )


__all__ = ["create_chat_service"]
