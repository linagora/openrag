"""Contract every registered chunker must meet."""

from __future__ import annotations

import pytest
from core.chunking import chunking_registry
from core.models.document import ProcessedDocument, TextBlock


def _words(text: str) -> int:
    return len(text.split())


_TABLE = "| Col | Val |\n|-----|-----|\n" + "\n".join(f"| Row{i} | {' '.join(['x'] * 40)} |" for i in range(6))
_DOCUMENT = ProcessedDocument(
    document_id="d1",
    text_blocks=[
        TextBlock(
            text=(
                "# Title\n\n"
                + " ".join(f"word{i}." for i in range(120))
                + "\n\n## Section\n\n"
                + " ".join(f"more{i}." for i in range(120))
                + f"\n\n{_TABLE}\n\nClosing sentence."
            ),
            page_number=1,
        )
    ],
    metadata={"filename": "doc.md"},
)


@pytest.mark.parametrize("name", chunking_registry.list_registered())
def test_every_chunker_stores_each_chunk_token_count(name):
    """The embedder-window checks read ``token_count`` instead of re-tokenising
    each chunk. A chunk without one is invisible to the overflow warning and has
    to be measured by the check that fails a file before the store."""
    chunker = chunking_registry.create(name, chunk_size=60, chunk_overlap_rate=0.0, length_function=_words)

    chunks = chunker.chunk(_DOCUMENT, partition="p")

    assert chunks
    assert [c.token_count for c in chunks] == [_words(c.text) for c in chunks]
