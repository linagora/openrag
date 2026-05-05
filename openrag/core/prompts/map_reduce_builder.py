"""Map-reduce prompt builder.

The orchestrator (Phase 8) loops over chunks, calls the LLM with these
messages, and reduces the structured outputs. The system + user-template
strings live here so they're testable in isolation and can be evolved
without touching pipeline code.
"""

from __future__ import annotations

SYSTEM_PROMPT_MAP = """You are an AI assistant specialized in extracting and synthesizing relevant information from text.

Your task:
1. Analyze the provided text in relation to the user's question
2. Extract only the essential information that directly addresses the query
3. Preserve necessary context (Key words, project names or initiatives, dates, etc.) to maintain accuracy and clarity of the summary for it to be self-understandable

Guidelines:
- Present information clearly and concisely without unnecessary rephrasing or commentary
- Focus on precision: include what matters, exclude what doesn't.
- If a document does not have any relevant content with respect to the query, classify it as irrelevant without providing a `synthesis`.
"""

USER_PROMPT_TEMPLATE = """
Here is a text:
{content}

From this document, identify and comprehensively summarize the information useful for answering the following question:
{query}
"""


def build_map_messages(query: str, content: str) -> list[dict[str, str]]:
    """Build the system+user message list for one map-step LLM call."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT_MAP},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(query=query, content=content)},
    ]
