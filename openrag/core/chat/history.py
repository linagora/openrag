"""The conversation as the request sends it: windowing, the pinned system prompt, cleanup.

The service is stateless, so the history is whatever the client sends on each
turn. These functions decide which part of it the contextualizer and the
answering model see.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()


def history_window(messages: list[dict], depth: int) -> list[dict]:
    """The last *depth* messages, widened to the latest message of the user.

    A conversation may end with the results of tool calls (an agentic loop):
    the request of the user they serve stays in the window, and the window
    never opens on a ``tool`` message cut from the assistant turn that called
    it.
    """
    start = max(0, len(messages) - depth)
    latest_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    if latest_user is not None and latest_user < start:
        start = latest_user
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    return messages[start:]


def latest_user_query(messages: list[dict]) -> str:
    """Return the latest user text without retaining any other message content."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def split_leading_system_prompt(raw_messages: list[dict], truncated: list[dict]) -> tuple[str | None, list[dict]]:
    """Pull a client-pinned leading system prompt out of ``raw_messages``.

    A leading run of ``role="system"`` messages in ``raw_messages`` (the
    untruncated payload) is a pinned instruction, not a chat turn. ``truncated``
    is a tail slice of ``raw_messages`` (``raw_messages[-depth:]``), so only the
    portion of that leading run still inside the tail is stripped from it — a
    system message elsewhere in history that merely lands first after
    chat_history_depth truncation is never mistaken for the pin and dropped.

    ``content`` is read defensively: ``OpenAIMessage`` allows a null/absent
    content (the assistant turn carrying ``tool_calls``), and the router dumps
    with ``exclude_none=True``, so the key is genuinely optional. A content-free
    system message still counts toward the leading run — it is stripped from the
    history like its siblings — it just contributes nothing to the pin.
    """
    parts: list[str] = []
    i = 0
    while i < len(raw_messages) and raw_messages[i]["role"] == "system":
        content = raw_messages[i].get("content")
        if content:
            parts.append(content)
        i += 1

    offset = len(raw_messages) - len(truncated)
    strip = max(0, i - offset)

    return ("\n\n".join(parts) if parts else None), truncated[strip:]


def _is_empty_content(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not content
    return False


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Replace empty-content ``role="assistant"`` messages with a placeholder.

    Some LLMs reject an assistant turn with no content. Substituting a
    placeholder preserves role alternation (no consecutive same-role
    messages are created). This should normally never happen, but we do it
    as a safeguard against a client sending a malformed history.

    An assistant message carrying ``tool_calls`` / ``function_call`` is
    legitimately content-free and is left untouched.
    """
    result = []
    replaced_count = 0
    for msg in messages:
        should_replace_msg = (
            msg.get("role") == "assistant"
            and not msg.get("tool_calls")
            and not msg.get("function_call")
            and _is_empty_content(msg.get("content"))
        )
        if should_replace_msg:
            result.append({**msg, "content": "NO_CONTENT"})
            replaced_count += 1
        else:
            result.append(msg)
    if replaced_count:
        logger.warning(
            "Replaced empty assistant message(s) with NO_CONTENT placeholder before LLM call — "
            "this should normally never happen; likely a client-side history persistence "
            "or streaming response issue",
            replaced_count=replaced_count,
        )
    return result


__all__ = ["history_window", "latest_user_query", "sanitize_messages", "split_leading_system_prompt"]
