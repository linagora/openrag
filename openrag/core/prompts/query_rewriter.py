"""Query-rewriting prompt builders for HyDe and Multi-Query retrieval.

Templates ship with the package under ``openrag/prompts/templates/`` and are
loaded via ``template_loader``. These functions are pure: they take a template
string + substitution variables and return the formatted prompt.

Template variables expected:
    HyDe template: ``{question}``
    Multi-query template: ``{query}``, ``{k_queries}``

The multi-query helper also exposes the ``[SEP]`` separator used to split the
LLM response into individual queries.
"""

from __future__ import annotations

MULTI_QUERY_SEPARATOR = "[SEP]"


def build_hyde_prompt(template: str, query: str) -> str:
    """Format a HyDe prompt. ``template`` must contain ``{question}``."""
    return template.format(question=query)


def build_multi_query_prompt(template: str, query: str, k_queries: int) -> str:
    """Format a multi-query prompt. ``template`` must contain ``{query}`` and ``{k_queries}``."""
    return template.format(query=query, k_queries=k_queries)


def split_multi_query_response(response: str, separator: str = MULTI_QUERY_SEPARATOR) -> list[str]:
    """Split an LLM multi-query response into individual queries.

    Drops empty entries and trims surrounding whitespace.
    """
    return [q.strip() for q in response.split(separator) if q.strip()]
