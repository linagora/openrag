"""Chunk-contextualization prompt builder.

Pure helpers extracted from ``components/indexer/chunker/chunker.py``. They
produce the system+user message pair sent to the LLM when generating
chunk-level context, and assemble the final wrapped chunk text used downstream.

Format strings:
    BASE_CHUNK_FORMAT — chunk wrapping when no LLM context is generated
    CHUNK_FORMAT      — chunk wrapping with leading [CONTEXT] block
"""

from __future__ import annotations

BASE_CHUNK_FORMAT = "* filename: {filename}\n\n[CHUNK_START]\n\n{content}\n\n[CHUNK_END]"
CHUNK_FORMAT = "[CONTEXT]\n\n{chunk_context}\n\n" + BASE_CHUNK_FORMAT


def build_user_message(
    filename: str,
    first_chunks_text: list[str],
    prev_chunks_text: list[str],
    current_chunk_text: str,
    lang: str = "en",
) -> str:
    """Render the user-message body for a single chunk-contextualization call.

    The system prompt is loaded from disk (``CHUNK_CONTEXTUALIZER_PROMPT``) and
    paired with this user message by the caller.
    """
    first = "\n--\n".join(first_chunks_text)
    previous = "\n--\n".join(prev_chunks_text)
    return (
        "\n"
        "        Here is the context to consider for generating the context:\n"
        f"        - Filename: {filename}\n"
        "        - First chunks:\n"
        f"        {first}\n\n"
        "        - Previous chunks:\n"
        f"        {previous}\n\n"
        f"        Here is the current chunk to contextualize strictly in this {lang} language:\n"
        "        - Current chunk:\n\n"
        f"        {current_chunk_text}\n        "
    )


def build_messages(
    system_prompt: str,
    filename: str,
    first_chunks_text: list[str],
    prev_chunks_text: list[str],
    current_chunk_text: str,
    lang: str = "en",
) -> list[dict[str, str]]:
    """Build the system+user message list for a chunk-contextualization call."""
    user = build_user_message(
        filename=filename,
        first_chunks_text=first_chunks_text,
        prev_chunks_text=prev_chunks_text,
        current_chunk_text=current_chunk_text,
        lang=lang,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]


def wrap_chunk_with_context(content: str, filename: str, chunk_context: str = "") -> str:
    """Wrap a chunk in the ``[CONTEXT] ... [CHUNK_START] ... [CHUNK_END]`` envelope.

    If ``chunk_context`` is empty or whitespace-only, only the BASE_CHUNK_FORMAT
    (no [CONTEXT] block) is used — preserves the legacy behavior for chunkers
    that don't run contextualization.
    """
    if chunk_context and chunk_context.strip():
        return CHUNK_FORMAT.format(content=content, chunk_context=chunk_context, filename=filename)
    return BASE_CHUNK_FORMAT.format(content=content, filename=filename)
