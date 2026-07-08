"""Retrieval query domain model."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RetrievalQuery(BaseModel):
    """A user query with retrieval parameters."""

    text: str
    partition: str = "default"
    top_k: int = 10
    similarity_threshold: float = 0.6
    filters: dict[str, Any] = Field(default_factory=dict)
    include_related: bool = False
    include_ancestors: bool = False
    related_limit: int = 10
    max_ancestor_depth: int | None = None
    with_surrounding_chunks: bool = True
    rerank: bool = True


class TemporalPredicate(BaseModel):
    """A single date constraint on a document's creation date.

    Multiple predicates on the same ``Query`` are AND-combined. Closed
    ranges (e.g. "last month") are encoded as two predicates, one per side.
    """

    field: Literal["created_at"] = Field(
        default="created_at",
        description="Document metadata field to filter on. Always `created_at` for now.",
    )
    operator: Literal[">", "<", ">=", "<="] = Field(
        description="Comparison operator applied to the date field.",
    )
    value: str = Field(
        description='ISO 8601 datetime with timezone, e.g. "2026-03-15T00:00:00+00:00".',
    )


class Query(BaseModel):
    """A single vector-database search query plus optional temporal filters.

    Two predicates yield an AND-range; an exclusion range (e.g. "last year
    except March") is expressed as two separate ``Query`` objects.
    """

    query: str = Field(
        description="A semantically enriched, descriptive query for vector similarity search.",
    )
    temporal_filters: list[TemporalPredicate] | None = Field(
        default=None,
        description="Date predicates on `created_at`, AND-combined.",
    )

    def to_milvus_filter(self) -> str | None:
        """Render the AND-combined predicates as a Milvus filter expression.

        Pydantic validates the field/operator types up front. The ``value``
        field is parsed as ISO 8601 here defensively — predicates with an
        unparseable value are dropped rather than crashing the search.
        """
        if not self.temporal_filters:
            return None
        parts: list[str] = []
        for p in self.temporal_filters:
            try:
                parsed = datetime.fromisoformat(p.value)
            except (TypeError, ValueError):
                logger.warning(
                    "Dropping temporal predicate with non-ISO value: field=%s operator=%s value=%r",
                    p.field,
                    p.operator,
                    p.value,
                )
                continue
            if parsed.tzinfo is None:
                logger.warning(
                    "Dropping temporal predicate without timezone: field=%s operator=%s value=%r",
                    p.field,
                    p.operator,
                    p.value,
                )
                continue
            parts.append(f'{p.field} {p.operator} ISO "{p.value}"')
        if not parts:
            return None
        return " and ".join(parts)

    def __str__(self) -> str:
        return f"Query: {self.query}, Filter: {self.to_milvus_filter()}"


class SearchQueries(BaseModel):
    """Collection of sub-queries produced by query decomposition."""

    query_list: list[Query] = Field(..., description="Search sub-queries to retrieve relevant documents.")

    def __str__(self) -> str:
        return " --- ".join(str(q) for q in self.query_list)
