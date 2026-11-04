"""Chat-completion prompt builder.

Pure helpers extracted from ``components/utils.py`` and ``components/pipeline.py``:

* ``format_context``         — fit document snippets into a token budget,
                                numbering each as ``[Source N]``.
* ``describe_source``        — the provenance line (file name, creation date)
                                a source block carries under its marker.
* ``format_web_context``     — same, for web-search results, with continuous
                                numbering across RAG and web sources.
* ``prepend_system_prompt``  — clone a message list and prepend a system
                                prompt rendered against ``context`` and
                                ``current_date``.
* ``message_tokens``         — tokens one chat message takes in the prompt.
* ``tool_definition_tokens`` — tokens the tool definitions a chat request
                                forwards take in the prompt.
* ``SOURCE_SEPARATOR``       — separator emitted between consecutive sources.

Tokenizers are injected as ``Callable[[str], int]`` so this module stays pure
(no LLM client, no LangChain).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from core.utils.text import neutralize_prompt_control_tokens, sanitize_text

SOURCE_SEPARATOR = "-" * 10 + "\n\n"
EMPTY_CONTEXT_MESSAGE = "No document found from the database"

#: Fields of a chat request, besides its messages, that the provider renders
#: into the prompt.
TOOL_DEFINITION_FIELDS = ("tools", "functions")

_UNSAFE_PROMPT_CLOSE_TAG_RE = re.compile(r"</unsafe_custom_prompt>", re.IGNORECASE)


class WebSourceLike(Protocol):
    """Minimal shape needed from a web-search result."""

    title: str
    url: str
    snippet: str
    content: str | None


def describe_source(metadata: dict | None) -> str:
    """One line of provenance for a source block: its file name and creation date.

    The chunk text itself carries no date, so without this line the answering
    LLM cannot tell yesterday's meeting notes from last year's, and "summarize
    the last meeting" is answered from whichever one ranked first. Returns
    ``""`` when the metadata carries neither, so undated sources render as
    before. The values are untrusted (upload metadata) and are neutralized like
    the content.
    """
    if not metadata:
        return ""
    parts: list[str] = []
    filename = metadata.get("filename") or metadata.get("original_filename")
    if filename:
        parts.append(f"File: {filename}")
    created_at = _format_created_at(metadata.get("created_at"))
    if created_at:
        parts.append(f"Created: {created_at}")
    return neutralize_prompt_control_tokens(" | ".join(parts))


def _format_created_at(value: object) -> str:
    """``2026-09-22 14:51 UTC`` from an ISO 8601 string or datetime; ``""`` otherwise.

    Minute precision is enough to order two meetings of the same day and keeps
    the line short; the raw ISO form with microseconds is what the API returns.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return ""
    else:
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def format_context(
    texts: list[str],
    max_context_tokens: int | None,
    length_function: Callable[[str], int],
    *,
    max_sources: int | None = None,
    number_sources: bool = True,
    headers: list[str] | None = None,
) -> tuple[str, list[int]]:
    """Render ``texts`` as numbered ``[Source N]`` blocks within a token budget.

    ``texts`` are taken in order, best-ranked first. One that doesn't fit in
    what's left of the budget is skipped, not the end of the list: a shorter
    one after it may still fit.

    Args:
        texts: Document texts (e.g. ``[d.page_content for d in docs]``).
        max_context_tokens: Maximum total tokens for the context; ``None`` for
            no token limit.
        length_function: Token counter, e.g. ``llm.get_num_tokens``.
        max_sources: Maximum number of sources; ``None`` for no limit.
        number_sources: If ``True``, prefix each block with ``[Source N]\\n``.
        headers: Optional per-text provenance line (see ``describe_source``),
            rendered between the ``[Source N]`` marker and the content and
            counted in the budget. Same length as ``texts``; an empty entry
            renders nothing.

    Returns:
        ``(formatted_text, included_indices)`` — ``included_indices`` is the
        positions in ``texts`` that fit within the budget; callers use it to
        filter associated metadata down to the same set.
    """
    if not texts:
        return EMPTY_CONTEXT_MESSAGE, []
    if headers is not None and len(headers) != len(texts):
        raise ValueError("headers must have one entry per text")

    reduced: list[str] = []
    included: list[int] = []
    total_tokens = 0

    for i, text in enumerate(texts):
        if max_sources is not None and len(reduced) >= max_sources:
            break
        prefix = f"[Source {len(reduced) + 1}]\n" if number_sources else ""
        header = headers[i] if headers else ""
        if header:
            # Already neutralized by describe_source; guard a caller-built one too.
            prefix += f"{neutralize_prompt_control_tokens(header)}\n"
        # Neutralize control tokens so a poisoned document cannot forge a
        # [Source N] block, inject a [Sources: ...] citation tag, or fake the
        # inter-source separator. The [Source N] prefix is the only trusted marker.
        content = neutralize_prompt_control_tokens(text)
        n_tokens = length_function(content)
        if prefix:
            n_tokens += length_function(prefix)
        if reduced:
            n_tokens += length_function(SOURCE_SEPARATOR)
        if max_context_tokens is not None and total_tokens + n_tokens > max_context_tokens:
            continue
        reduced.append(f"{prefix}{content}")
        included.append(i)
        total_tokens += n_tokens

    return SOURCE_SEPARATOR.join(reduced), included


def format_web_context(
    web_results: list[WebSourceLike],
    length_function: Callable[[str], int],
    *,
    start_index: int = 1,
    max_tokens: int = 2000,
) -> tuple[str, list[int], int]:
    """Render web results as numbered ``[Source N]`` blocks within a token budget.

    Uses ``result.content`` when present, falling back to ``result.snippet``.

    Args:
        web_results: Web-search result objects (matching ``WebSourceLike``).
        length_function: Token counter.
        start_index: First source number — set to ``len(rag_sources) + 1`` so
                     web sources continue numbering after RAG sources.
        max_tokens: Maximum total tokens for the web context.

    Returns:
        ``(formatted_text, source_numbers_used, total_tokens_used)``.
    """
    if not web_results:
        return "", [], 0

    parts: list[str] = []
    source_numbers: list[int] = []
    total_tokens = 0

    for i, result in enumerate(web_results):
        n = start_index + i
        title = neutralize_prompt_control_tokens(sanitize_text(result.title))
        body_raw = result.content if result.content else result.snippet
        body = neutralize_prompt_control_tokens(sanitize_text(body_raw)) if body_raw else ""
        block = f"[Source {n}]\n{title}\n{body}"
        block_tokens = length_function(block)
        if parts:
            block_tokens += length_function(SOURCE_SEPARATOR)
        if total_tokens + block_tokens > max_tokens:
            break
        parts.append(block)
        source_numbers.append(n)
        total_tokens += block_tokens

    return SOURCE_SEPARATOR.join(parts), source_numbers, total_tokens


def prepend_system_prompt(
    messages: list[dict],
    system_template: str,
    *,
    context: str,
    current_date: str,
    custom_prompt: str | None = None,
) -> list[dict]:
    """Return a deep-copied message list with a rendered system prompt prepended.

    ``system_template`` must contain ``{context}`` and ``{current_date}``. A
    ``{custom_prompt}`` placeholder is optional — where present, ``custom_prompt``
    (e.g. a client-pinned custom system instruction) is given its own
    ``# User defined an unsafe_custom_prompt`` heading, wrapping an
    ``<unsafe_custom_prompt>`` block, and spliced in there; templates without
    the placeholder silently ignore ``custom_prompt``. The heading and tag both
    flag the content as untrusted input to the LLM, not an authoritative
    instruction — paired with a template rule instructing the model to keep
    following its core rules regardless of what this block says.
    ``custom_prompt`` is run through ``neutralize_prompt_control_tokens`` first,
    same as RAG/web context, so it cannot forge a ``[Source N]`` /
    ``[Sources: ...]`` marker.
    """
    out = copy.deepcopy(messages)
    # A literal closing tag could break out of the untrusted-content wrapper
    # and have trailing attacker text read as if outside it.
    safe_custom_prompt = (
        _UNSAFE_PROMPT_CLOSE_TAG_RE.sub(
            "&lt;/unsafe_custom_prompt&gt;", neutralize_prompt_control_tokens(custom_prompt)
        )
        if custom_prompt
        else custom_prompt
    )
    custom_prompt_block = (
        f"\n# User defined an unsafe_custom_prompt\n<unsafe_custom_prompt>\n{safe_custom_prompt}\n</unsafe_custom_prompt>\n"
        if custom_prompt
        else ""
    )
    rendered = system_template.format(
        context=context,
        current_date=current_date,
        custom_prompt=custom_prompt_block,
    )
    out.insert(0, {"role": "system", "content": rendered})
    return out


def message_tokens(message: dict, length_function: Callable[[str], int]) -> int:
    """Tokens *message* takes in the prompt: its text, plus 4 for the turn.

    The provider gets every other field of the message too, and renders the
    ones it knows into the prompt: an assistant turn's ``tool_calls`` /
    ``function_call``, a tool result's ``tool_call_id``. Those fields count as
    their JSON.
    """
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    tokens = length_function(content if isinstance(content, str) else "") + 4
    fields = {k: v for k, v in message.items() if k not in ("role", "content") and v is not None}
    if fields:
        tokens += length_function(json.dumps(fields, ensure_ascii=False, default=str))
    return tokens


def tool_definition_tokens(request: dict, length_function: Callable[[str], int]) -> int:
    """Tokens the tool definitions in a chat *request* take, as their JSON.

    OpenRag doesn't call tools itself, but forwards a client's definitions to
    the provider, which renders them into the prompt.
    """
    definitions = {k: request[k] for k in TOOL_DEFINITION_FIELDS if request.get(k)}
    return length_function(json.dumps(definitions, ensure_ascii=False, default=str)) if definitions else 0
