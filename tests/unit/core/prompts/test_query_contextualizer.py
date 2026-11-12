"""The query contextualizer's messages and the parsing of its answer."""

from datetime import UTC, datetime

import pytest
from core.prompts.query_contextualizer import (
    QUERY_JSON_HINT,
    build_contextualizer_messages,
    format_chat_history,
    parse_search_queries,
)
from pydantic import ValidationError

_TEMPLATE = "Language: {query_language}\nCurrent date: {current_date}\n{calendar_anchors}"


def test_query_json_hint_leaves_the_temporal_fields_open():
    """The hint is the last thing the model reads; a literal ``null`` there was
    copied verbatim and the date rules never fired."""
    assert '"temporal_filters": null' not in QUERY_JSON_HINT
    assert '"date_order"' in QUERY_JSON_HINT


def test_format_chat_history_leaves_tool_results_out():
    messages = [
        {"role": "user", "content": "Reply to Claire"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "SECRET tool output"},
    ]
    assert format_chat_history(messages) == "user: Reply to Claire\nassistant: \n"


def test_build_contextualizer_messages_renders_the_template_and_appends_the_hint():
    now = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)

    system, user = build_contextualizer_messages(
        _TEMPLATE, messages=[{"role": "user", "content": "last week's report"}], query_language="en", now=now
    )

    assert system["role"] == "system"
    assert system["content"].startswith("Language: en\nCurrent date: Wednesday, September 16, 2026, 23:30:00\n")
    assert "- last week [2026-09-07, 2026-09-14)" in system["content"]
    assert system["content"].endswith(QUERY_JSON_HINT)
    assert user == {"role": "user", "content": "Here is the chat history: \nuser: last week's report\n\n"}


def test_parse_search_queries_reads_json_wrapped_in_prose():
    queries = parse_search_queries('Sure: {"query_list": [{"query": "notice period"}]} done')
    assert [q.query for q in queries.query_list] == ["notice period"]
    assert queries.requires_retrieval is True


def test_parse_search_queries_raises_on_an_invalid_answer():
    with pytest.raises(ValidationError):
        parse_search_queries('{"intent": "other"}')
