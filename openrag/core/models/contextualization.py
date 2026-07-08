"""Contextualized query models — query rewriting, HyDE, reasoning."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContextualizedQuery(BaseModel):
    """A user query after contextualization by the LLM.

    May include rewritten variants, a hypothetical document (HyDE),
    and sub-queries for multi-step retrieval.
    """

    original: str = ""
    query_list: list[str] = Field(default_factory=list)
    intent: str = "qa"
    hypothetical_doc: str | None = None
    sub_queries: list[str] = Field(default_factory=list)
