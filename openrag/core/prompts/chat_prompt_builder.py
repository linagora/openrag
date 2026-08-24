"""Chat-completion prompt builder.

Pure helpers extracted from ``components/utils.py`` and ``components/pipeline.py``:

* ``format_context``         — fit document snippets into a token budget,
                                numbering each as ``[Source N]``.
* ``format_web_context``     — same, for web-search results, with continuous
                                numbering across RAG and web sources.
* ``prepend_system_prompt``  — clone a message list and prepend a system
                                prompt rendered against ``context`` and
                                ``current_date``.
* ``SOURCE_SEPARATOR``       — separator emitted between consecutive sources.

Tokenizers are injected as ``Callable[[str], int]`` so this module stays pure
(no LLM client, no LangChain).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Protocol

from core.utils.text import neutralize_prompt_control_tokens, sanitize_text

SOURCE_SEPARATOR = "-" * 10 + "\n\n"
EMPTY_CONTEXT_MESSAGE = "No document found from the database"

_UNSAFE_PROMPT_CLOSE_TAG_RE = re.compile(r"</unsafe_custom_prompt>", re.IGNORECASE)


class WebSourceLike(Protocol):
    """Minimal shape needed from a web-search result."""

    title: str
    url: str
    snippet: str
    content: str | None


def format_context(
    texts: list[str],
    max_context_tokens: int,
    length_function: Callable[[str], int],
    *,
    number_sources: bool = True,
) -> tuple[str, list[int]]:
    """Render ``texts`` as numbered ``[Source N]`` blocks within a token budget.

    Args:
        texts: Document texts (e.g. ``[d.page_content for d in docs]``).
        max_context_tokens: Maximum total tokens for the context.
        length_function: Token counter, e.g. ``llm.get_num_tokens``.
        number_sources: If ``True``, prefix each block with ``[Source N]\\n``.

    Returns:
        ``(formatted_text, included_indices)`` — ``included_indices`` is the
        positions in ``texts`` that fit within the budget; callers use it to
        filter associated metadata down to the same set.
    """
    if not texts:
        return EMPTY_CONTEXT_MESSAGE, []

    reduced: list[str] = []
    included: list[int] = []
    total_tokens = 0

    for i, text in enumerate(texts):
        prefix = f"[Source {len(reduced) + 1}]\n" if number_sources else ""
        # Neutralize control tokens so a poisoned document cannot forge a
        # [Source N] block, inject a [Sources: ...] citation tag, or fake the
        # inter-source separator. The [Source N] prefix is the only trusted marker.
        content = neutralize_prompt_control_tokens(text)
        n_tokens = length_function(content)
        if prefix:
            n_tokens += length_function(prefix)
        if total_tokens + n_tokens > max_context_tokens:
            break
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
    # Same treatment as RAG/web context: a client-supplied custom_prompt is
    # untrusted text and must not be able to forge a [Source N] block or a
    # [Sources: ...] citation tag next to the real Context.
    # A literal closing tag could also break out of the untrusted-content
    # wrapper and have trailing attacker text read as if outside it.
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
