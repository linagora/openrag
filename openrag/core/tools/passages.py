"""How a tool shows a document chunk to the model: its ``[Source N]`` marker, where it comes from, its text.

The marker is the only trusted part. Everything after it comes from the
document or its upload metadata, so it is flattened and defanged as
``format_context`` does for the answer prompt: a chunk can't forge a marker, a
citation tag or a separator, and a file name or id can't add a line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from core.prompts import SOURCE_SEPARATOR, describe_source
from core.utils.text import neutralize_prompt_control_tokens

if TYPE_CHECKING:
    from core.models.chunk import Chunk

_MAX_ID_CHARS = 150

#: A passage's marker, on its own line: a chunk's text can't forge one (``passage`` defangs it).
_MARKER = re.compile(r"^\[Source (\d+)\]$", re.MULTILINE)


def passage(number: int, chunk: Chunk, *, excerpt_chars: int | None = None) -> str:
    """*chunk* as ``[Source number]``: its date and file, the ids to read it by, its text.

    With *excerpt_chars* the text is cut to about that many characters, at a
    word boundary: an excerpt locates the passage, ``read_passage`` reads it.
    """
    lines = [f"[Source {number}]"]
    provenance = describe_source(chunk.metadata)
    if provenance:
        lines.append(provenance)
    lines.append(f"Partition: {one_line(chunk.partition)} | File id: {one_line(chunk.document_id)}")
    text = chunk.text if excerpt_chars is None else excerpt(chunk.text, excerpt_chars)
    lines.append(neutralize_prompt_control_tokens(text))
    return "\n".join(lines)


def passages(blocks: list[str]) -> str:
    """Passage blocks, separated as in the answer prompt."""
    return SOURCE_SEPARATOR.join(blocks)


def shown_sources(text: str) -> list[int]:
    """The ``[Source N]`` numbers *text* shows passages under, in its order, each once."""
    return list(dict.fromkeys(int(number) for number in _MARKER.findall(text)))


def excerpt(text: str, max_chars: int) -> str:
    """*text* on one line, cut to at most *max_chars* characters at a word boundary, ``…`` marking the cut."""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    cut = flat.rfind(" ", 0, max_chars)
    return flat[: cut if cut > max_chars // 2 else max_chars] + " …"


def one_line(value: object) -> str:
    """*value* as untrusted text on one line: flattened, without the field separator, capped and defanged."""
    flat = " ".join(str(value).replace("|", " ").split())[:_MAX_ID_CHARS]
    return neutralize_prompt_control_tokens(flat)


__all__ = ["excerpt", "one_line", "passage", "passages", "shown_sources"]
