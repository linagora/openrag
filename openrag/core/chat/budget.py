"""Fitting the sources into what the answering model's context window leaves.

Two steps, both pure:

1. ``fit_sources`` picks up to ``top_n`` documents that fit in the room the
   instructions, the conversation and the tool definitions leave, each block
   sized as ``documents_context`` renders it.
2. ``fit_prompt`` measures the rendered prompt, which also holds what step 1
   could not size (the separators, the web results' final numbering), and
   drops sources, documents first, until it fits.

The token counter and the renderer are passed in, so the same rules serve
chat messages and text-completion prompts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from core.prompts.chat_prompt_builder import describe_source, format_context
from core.utils.exceptions import ContextWindowExceededError

Rendered = TypeVar("Rendered")


def fit_sources(docs: list, *, top_n: int, headroom: int | None, length_function: Callable[[str], int]) -> list[int]:
    """Positions of up to *top_n* of *docs* that fit in *headroom* tokens.

    *headroom* is ``None`` when the window is unknown: only *top_n* applies.
    Each block is sized with its provenance line, as ``documents_context``
    renders it.
    """
    _, included = format_context(
        [doc.page_content for doc in docs],
        max_context_tokens=headroom,
        length_function=length_function,
        max_sources=top_n,
        headers=[describe_source(doc.metadata) for doc in docs],
    )
    return included


def fit_prompt(
    assemble: Callable[[list, list], tuple[Rendered, list]],
    docs: list,
    web: list,
    *,
    measure: Callable[[Rendered], int],
    max_tokens: int | None,
) -> tuple[Rendered, list, list]:
    """Render with *docs* and *web*, dropping sources until *measure* fits in *max_tokens*.

    *assemble(docs, web)* returns the rendered prompt and the web results it
    could show. Documents are dropped first, from the end; the web candidates
    stay whole meanwhile, so a shorter document list can make room for more of
    them. Once no document is left, the web results shown are dropped from the
    end. With no source left the prompt can still overflow, by what the room
    didn't measure (the no-document message): that raises
    ``ContextWindowExceededError``.

    Returns the rendered prompt, the documents in it and the web results shown.
    *max_tokens* ``None`` means the window is unknown: nothing is dropped.
    """
    rendered, web_shown = assemble(docs, web)
    while max_tokens is not None:
        sent = measure(rendered)
        if sent <= max_tokens:
            break
        if not (docs or web_shown):
            raise ContextWindowExceededError(sent, max_tokens)
        if docs:
            docs = docs[:-1]
        else:
            web = web_shown[:-1]
        rendered, web_shown = assemble(docs, web)
    return rendered, docs, web_shown


__all__ = ["fit_prompt", "fit_sources"]
