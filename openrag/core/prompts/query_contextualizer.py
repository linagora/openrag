"""The query contextualizer's messages and the parsing of its answer.

The contextualizer reads the chat history and returns ``SearchQueries``: the
sub-queries to search, each with its date predicates and order, and whether the
latest message needs a search at all. Its template is a prompt of type
``query_contextualizer`` (admin-editable); this module renders it with the
three placeholders that type allows and appends ``QUERY_JSON_HINT``.
"""

from __future__ import annotations

from datetime import datetime

from core.models.query import SearchQueries
from core.prompts.calendar_anchors import calendar_anchors
from core.utils.string_utils import json_object_slice

# Appended to the contextualizer prompt as a last reminder of the output shape.
# It must leave every rule-driven field open: an earlier version showed
# `"temporal_filters": null` as the only form and models copied it literally,
# so the date rules above it were never applied.
QUERY_JSON_HINT = (
    "\n\nClassify the complete latest user message conservatively. Retrieval is the default. "
    "Only a message consisting exclusively of a greeting or salutation, gratitude, farewell, or a question about "
    "the assistant's capabilities may skip retrieval. A factual, informational, analytical, ambiguous, or mixed "
    "message must require retrieval, even when it has no question mark or also contains a social phrase. "
    "A brief acknowledgement after a social, wellbeing, or feedback question remains gratitude and may skip "
    "retrieval. It requires retrieval only when it accepts factual, informational, analytical, or document-backed "
    "continuation from the assistant's previous turn. "
    "Respond ONLY with one of these JSON forms: "
    '{"intent": "greeting", "requires_retrieval": false, "query_list": []} for an exclusively casual message '
    "(using gratitude, farewell, or capability instead of greeting when appropriate), or "
    '{"intent": "other", "requires_retrieval": true, '
    '"query_list": [{"query": "<search query>", "temporal_filters": <predicates per the rules above, or null>, '
    '"date_order": <"newest" | "oldest" | null, per the rules above>}]} for everything else. '
    "If uncertain, use intent other and require retrieval."
)


def format_chat_history(messages: list[dict]) -> str:
    """The history as ``role: content`` lines.

    The results of tools are data the app gave, not turns of the
    conversation: they do not shape the search.
    """
    return "".join(f"{m['role']}: {m.get('content') or ''}\n" for m in messages if m.get("role") != "tool")


def build_contextualizer_messages(
    template: str, *, messages: list[dict], query_language: str | None, now: datetime
) -> list[dict]:
    """The contextualizer's system and user messages for *messages*.

    *now* must be in UTC: the calendar anchors are compared against UTC
    ``created_at`` timestamps, and near midnight the local date is a different
    day. The anchors are pre-computed week, month and year ranges, so the model
    doesn't do the calendar arithmetic itself.
    """
    prompt = template.format(
        query_language=query_language,
        current_date=now.strftime("%A, %B %d, %Y, %H:%M:%S"),
        calendar_anchors=calendar_anchors(now),
    )
    return [
        {"role": "system", "content": prompt + QUERY_JSON_HINT},
        {"role": "user", "content": f"Here is the chat history: \n{format_chat_history(messages)}\n"},
    ]


def parse_search_queries(content: str) -> SearchQueries:
    """The contextualizer's answer as ``SearchQueries``; raises on an unparsable or invalid one."""
    return SearchQueries.model_validate_json(json_object_slice(content))


__all__ = ["QUERY_JSON_HINT", "build_contextualizer_messages", "format_chat_history", "parse_search_queries"]
