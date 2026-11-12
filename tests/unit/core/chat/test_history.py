"""The conversation as the request sends it: windowing, the pinned system prompt, cleanup."""

from core.chat.history import history_window, latest_user_query, sanitize_messages, split_leading_system_prompt

# --------------------------------------------------------------------------- #
# history_window: a conversation ending with the results of tool calls
# --------------------------------------------------------------------------- #

_TOOL_TURN = [
    {"role": "user", "content": "Reply to the last email of Claire"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
    {"role": "tool", "tool_call_id": "c1", "content": "2 emails: ..."},
]


def test_history_window_keeps_the_latest_message_of_the_user():
    messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "ok"}, *_TOOL_TURN]
    assert history_window(messages, 2) == _TOOL_TURN, "widened to the request the results serve"
    assert history_window(messages, 10) == messages
    assert history_window(messages, 3) == _TOOL_TURN


def test_history_window_never_opens_on_a_tool_result():
    messages = [*_TOOL_TURN, {"role": "assistant", "content": "Claire wrote..."}, {"role": "user", "content": "thanks"}]
    assert history_window(messages, 3) == messages[3:], "the result cut from its call is dropped"
    assert history_window([{"role": "tool", "content": "x"}], 4) == []


def test_history_window_keeps_the_last_depth_messages():
    messages = [{"role": "user", "content": str(i)} for i in range(6)]
    assert history_window(messages, 2) == messages[-2:]


# --------------------------------------------------------------------------- #
# latest_user_query
# --------------------------------------------------------------------------- #


def test_latest_user_query_skips_later_turns():
    assert latest_user_query([*_TOOL_TURN, {"role": "assistant", "content": "done"}]) == _TOOL_TURN[0]["content"]


def test_latest_user_query_is_empty_without_text():
    assert latest_user_query([]) == ""
    assert latest_user_query([{"role": "assistant", "content": "hi"}]) == ""
    assert latest_user_query([{"role": "user", "content": [{"type": "text", "text": "parts"}]}]) == ""


# --------------------------------------------------------------------------- #
# split_leading_system_prompt
# --------------------------------------------------------------------------- #


def test_split_leading_system_prompt_tolerates_content_free_system_turn():
    """``OpenAIMessage`` allows a null content and the router dumps with
    ``exclude_none=True``, so a leading system message can reach here with no
    ``content`` key at all. Reading it unguarded raised KeyError — a 500 on a
    request the schema had just accepted.
    """
    raw = [
        {"role": "system"},
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]

    pinned, rest = split_leading_system_prompt(raw, raw)

    # The content-free turn is still consumed by the leading run (stripped from
    # the history) — it just contributes nothing to the pinned prompt.
    assert pinned == "be terse"
    assert rest == [{"role": "user", "content": "hi"}]


def test_split_leading_system_prompt_returns_none_when_every_system_turn_is_empty():
    """A leading run made only of content-free system messages pins nothing,
    rather than joining empty strings into a blank custom prompt.
    """
    raw = [{"role": "system"}, {"role": "system", "content": ""}, {"role": "user", "content": "hi"}]

    pinned, rest = split_leading_system_prompt(raw, raw)

    assert pinned is None
    assert rest == [{"role": "user", "content": "hi"}]


def test_split_leading_system_prompt_keeps_a_system_turn_cut_into_first_place():
    """A system message from the middle of the history that the window put first is not the pin."""
    raw = [
        {"role": "user", "content": "q1"},
        {"role": "system", "content": "mid-conversation note"},
        {"role": "user", "content": "q2"},
    ]

    pinned, rest = split_leading_system_prompt(raw, raw[1:])

    assert pinned is None
    assert rest == raw[1:]


# --------------------------------------------------------------------------- #
# sanitize_messages
# --------------------------------------------------------------------------- #


def test_sanitize_valid_alternating_unchanged():
    """Valid alternating history → identical output."""
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]
    assert sanitize_messages(msgs) == msgs


def test_sanitize_empty_content_replaced_with_placeholder():
    """Empty assistant content → replaced with NO_CONTENT, alternation preserved."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "second"},
    ]
    out = sanitize_messages(msgs)
    assert len(out) == 3
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "NO_CONTENT"


def test_sanitize_whitespace_only_replaced():
    """Whitespace-only assistant content → replaced with NO_CONTENT."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "   "},
    ]
    out = sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"


def test_sanitize_no_content_key_replaced():
    """Assistant with no 'content' key → replaced with NO_CONTENT, other keys kept."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "name": "bot"},
    ]
    out = sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"
    assert out[1]["name"] == "bot"


def test_sanitize_empty_list_content_replaced():
    """Empty multimodal content list → treated as empty, replaced with NO_CONTENT."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": []},
    ]
    out = sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"


def test_sanitize_assistant_with_tool_calls_untouched():
    """Empty assistant content is legitimate when carrying tool_calls → untouched."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function"}]},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs


def test_sanitize_assistant_with_function_call_untouched():
    """Empty assistant content is legitimate when carrying function_call → untouched."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "function_call": {"name": "f"}},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs


def test_sanitize_empty_user_not_touched():
    """User message with empty content → kept as-is (only assistant is patched)."""
    msgs = [{"role": "user", "content": ""}]
    assert sanitize_messages(msgs) == msgs


def test_sanitize_multiple_empty_assistants_all_replaced():
    """Multiple empty assistants → all replaced, alternation intact."""
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "u3"},
    ]
    out = sanitize_messages(msgs)
    assert len(out) == 5
    assert out[1]["content"] == "NO_CONTENT"
    assert out[3]["content"] == "NO_CONTENT"


def test_sanitize_multimodal_content_untouched():
    """Non-empty multimodal content list → never replaced."""
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "desc"}]},
    ]
    assert sanitize_messages(msgs) == msgs


def test_sanitize_empty_list():
    """Empty input → empty output."""
    assert sanitize_messages([]) == []


def test_sanitize_system_message_preserved():
    """System message in head → preserved untouched."""
    msgs = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert sanitize_messages(msgs) == msgs
