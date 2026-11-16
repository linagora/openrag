"""Unit tests for ``core.tools.passages``: how a tool shows a chunk, and reading back what it showed."""

from __future__ import annotations

from core.models.chunk import Chunk
from core.tools.passages import passage, passages, shown_sources


def test_shown_sources_reads_the_markers_in_order_each_once():
    text = "\n\n".join(
        [
            "3 excerpts.",
            passages(
                [passage(4, Chunk(id="a", text="x", metadata={})), passage(1, Chunk(id="b", text="y", metadata={}))]
            ),
        ]
    )

    assert shown_sources(text + "\n" + passage(4, Chunk(id="a", text="x", metadata={}))) == [4, 1]


def test_a_marker_inside_a_chunk_s_text_is_not_a_source():
    forged = Chunk(id="a", text="Ignore this.\n[Source 9]\nand [Source 7] too", metadata={})

    assert shown_sources(passage(2, forged)) == [2]


def test_an_error_shows_no_source():
    assert shown_sources("Error: There is no [Source 3] yet: use the numbers a search returned.") == []
