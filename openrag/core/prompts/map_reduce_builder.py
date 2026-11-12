"""Map-reduce prompt builder.

The chat service loops over the retrieved chunks, calls the LLM once per chunk
with these messages, and keeps the chunks it judges relevant, replaced by their
summaries. The LLM answers in JSON: ``{"relevancy": bool, "summary": str}``.
"""

from __future__ import annotations

SYSTEM_PROMPT_MAP = """You are an AI assistant specialized in extracting and synthesizing relevant information from text.

Your task:
1. Analyze the provided text in relation to the user's question
2. Extract only the essential information that directly addresses the query
3. Preserve necessary context (key words, project names, dates) so the summary is self-understandable

Respond with a JSON object exactly matching this schema:
{"relevancy": <true|false>, "summary": "<summary text, empty string if not relevant>"}
Set relevancy=false (and summary="") if the text has no relevant content for the query."""

USER_PROMPT_TEMPLATE = """Here is a text:
{content}

From this document, identify and comprehensively summarize the information useful for answering the following question:
{query}"""


def build_map_messages(query: str, content: str) -> list[dict[str, str]]:
    """Build the system+user message list for one map-step LLM call."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT_MAP},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(query=query, content=content)},
    ]
