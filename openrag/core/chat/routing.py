"""Which route a chat request takes, from its model and its options.

The ``model`` names the scope: empty, missing or the configured LLM's name is
the LLM itself, ``openrag-<partition>`` and ``openrag-all`` are partitions
(resolved by the API, which checks access). Then the scope and
``metadata.websearch`` give the route:

========================  =================  ==============================
partitions                ``websearch``      route
========================  =================  ==============================
none                      off                ``DIRECT``: the LLM, no sources
none                      on                 ``WEB``: web results only, and
                                             ``DIRECT`` when there are none
some                      off                ``DOCUMENTS``
some                      on                 ``DOCUMENTS_AND_WEB``
========================  =================  ==============================

``rag_mode`` only matters on the routes with documents.
"""

from __future__ import annotations

from enum import StrEnum

from core.models.chat import ChatOptions


class Route(StrEnum):
    DIRECT = "direct"
    WEB = "web"
    DOCUMENTS = "documents"
    DOCUMENTS_AND_WEB = "documents_and_web"

    @property
    def searches(self) -> bool:
        """Whether this route searches anything before answering."""
        return self is not Route.DIRECT

    @property
    def has_documents(self) -> bool:
        return self in (Route.DOCUMENTS, Route.DOCUMENTS_AND_WEB)

    @property
    def has_web(self) -> bool:
        return self in (Route.WEB, Route.DOCUMENTS_AND_WEB)


def is_direct_llm_model(model: str | None, llm_model: str) -> bool:
    """True when *model* names the LLM itself rather than a partition."""
    return model is None or model == "" or model == llm_model


def resolve_route(partitions: list[str] | None, options: ChatOptions) -> Route:
    """The route for a request scoped to *partitions* (``None`` for none)."""
    if partitions is None:
        return Route.WEB if options.websearch else Route.DIRECT
    return Route.DOCUMENTS_AND_WEB if options.websearch else Route.DOCUMENTS


__all__ = ["Route", "is_direct_llm_model", "resolve_route"]
