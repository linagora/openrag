"""End-to-end tests for the RecursiveSplitter chunker."""

from __future__ import annotations

from core.chunking.recursive import RecursiveSplitter
from core.chunking.registry import chunking_registry
from core.models.chunk import ChunkType
from core.models.document import ProcessedDocument, TextBlock


def _word_tokens(text: str) -> int:
    return len(text.split())


def test_recursive_splitter_is_registered():
    assert "recursive_splitter" in chunking_registry


def test_recursive_splitter_chunks_simple_document():
    splitter = RecursiveSplitter(chunk_size=10, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text="alpha beta gamma\ndelta epsilon zeta\neta theta iota.", page_number=1)],
        metadata={"source": "test.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks
    assert all(c.partition == "p1" for c in chunks)
    assert all(c.document_id == "d1" for c in chunks)
    assert all(c.chunk_type == ChunkType.TEXT for c in chunks)


def test_recursive_splitter_emits_table_chunks():
    table = "| Col | Val |\n|-----|-----|\n" + "\n".join(f"| Group{i} | {' '.join(['x'] * 50)} |" for i in range(6))
    splitter = RecursiveSplitter(chunk_size=20, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=f"Some prose here.\n\n{table}\n\nMore prose.", page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")
    table_chunks = [c for c in chunks if c.chunk_type == ChunkType.TABLE]
    assert table_chunks, "expected at least one table-type chunk"


def test_recursive_splitter_skips_image_placeholder():
    placeholder_md = (
        "Real text first.\n\n<image_description>\n\n[Image Placeholder]\n\n</image_description>\n\nReal text after."
    )
    splitter = RecursiveSplitter(chunk_size=200, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=placeholder_md, page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert all(c.chunk_type != ChunkType.IMAGE_CAPTION for c in chunks)
    for c in chunks:
        assert "[image placeholder]" not in c.text.lower()


def test_recursive_splitter_metadata_passthrough():
    splitter = RecursiveSplitter(chunk_size=10, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text="alpha beta gamma delta epsilon", page_number=1)],
        metadata={"source": "test.md", "filename": "test.md", "tag": "v1"},
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks
    assert chunks[0].metadata.get("source") == "test.md"
    assert chunks[0].metadata.get("tag") == "v1"


def test_recursive_splitter_empty_document_returns_empty():
    splitter = RecursiveSplitter(chunk_size=10, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(document_id="d1", text_blocks=[])
    assert splitter.chunk(doc, partition="p1") == []


def test_recursive_splitter_requires_length_function():
    import pytest

    with pytest.raises(ValueError, match="length_function"):
        RecursiveSplitter(chunk_size=10, chunk_overlap_rate=0.0)


def test_recursive_splitter_joins_multi_block_document_with_synthetic_page_markers():
    """Multi-block docs need synthetic [PAGE_N] markers so chunks downstream
    of a page boundary report the right page. Cover that injection path in
    BaseChunker._content_from with a chunk_size small enough to force a split
    across pages."""
    block_text = " ".join([f"word{i}" for i in range(20)])
    splitter = RecursiveSplitter(chunk_size=8, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=block_text, page_number=1),
            TextBlock(text=block_text, page_number=2),
            TextBlock(text=block_text, page_number=3),
        ],
        metadata={"source": "multi.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks
    pages = {c.page_number for c in chunks}
    # First chunk(s) stay on page 1; once a [PAGE_N] marker lands inside a
    # chunk's content the next chunk resolves to >=2.
    assert 1 in pages
    assert any((p or 0) >= 2 for p in pages)


def test_recursive_splitter_inlines_small_table():
    """Tables under the inline threshold (<=100 length-function tokens) flow
    through the text path rather than emitting a standalone TABLE chunk."""
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    splitter = RecursiveSplitter(chunk_size=200, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=f"Lead-in.\n\n{table}\n\nTrailing.", page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks
    assert all(c.chunk_type != ChunkType.TABLE for c in chunks)


def test_recursive_splitter_image_caption_chunk_emitted_when_above_threshold():
    """Image_description blocks above the inline threshold land as their own
    chunks (chunk_type=image_caption) — covers the standalone-element path in
    _get_chunks's else branch."""
    long_caption = "lorem ipsum dolor sit amet " * 60  # well above inline threshold
    md = f"Some text.\n\n<image_description>\n{long_caption}\n</image_description>\n\nAfter."
    splitter = RecursiveSplitter(chunk_size=400, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=md, page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")
    image_chunks = [c for c in chunks if c.chunk_type == ChunkType.IMAGE_CAPTION]
    assert image_chunks, "expected at least one image_caption chunk"


def test_recursive_splitter_returns_empty_when_only_image_placeholder():
    """When the only element is a skipped image placeholder, _get_chunks
    produces nothing — exercise the `if not chunks: return []` guard."""
    splitter = RecursiveSplitter(chunk_size=200, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text="<image_description>\n[Image Placeholder]\n</image_description>", page_number=1),
        ],
    )
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks == []


def test_base_chunker_lazy_initializes_text_splitter():
    """A BaseChunker subclass that forgets to set self.text_splitter still
    works — split_text lazy-builds a default RecursiveCharacterTextSplitter."""
    from core.chunking.recursive import BaseChunker

    class BareChunker(BaseChunker):
        pass

    bare = BareChunker(chunk_size=12, chunk_overlap_rate=0.0, length_function=_word_tokens)
    assert bare.text_splitter is None
    pieces = bare.split_text("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu")
    assert pieces
    assert bare.text_splitter is not None  # cached after first call


def test_document_metadata_cannot_override_file_id_or_partition():
    """Reserved identity fields must win over arbitrary metadata keys
    (CodeRabbit #3)."""
    splitter = RecursiveSplitter(chunk_size=20, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="real-doc-id",
        text_blocks=[TextBlock(text="alpha beta gamma delta", page_number=1)],
        metadata={
            "file_id": "MALICIOUS_OVERRIDE",
            "partition": "MALICIOUS_PARTITION",
            "source": "ok.md",
        },
    )
    chunks = splitter.chunk(doc, partition="real-partition")
    assert chunks
    for c in chunks:
        assert c.document_id == "real-doc-id"
        assert c.partition == "real-partition"
        # Other metadata keys still flow through.
        assert c.metadata.get("source") == "ok.md"


def test_document_metadata_cannot_override_chunk_type_or_page():
    """Per-chunk reserved keys (chunk_type, page, page_content) must win
    over `document.metadata`. A poison `chunk_type` value would otherwise
    crash `chunk()` when ChunkType(...) is constructed (ultrareview)."""
    splitter = RecursiveSplitter(chunk_size=20, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text="alpha beta gamma delta epsilon", page_number=2)],
        metadata={
            "chunk_type": "POISON",
            "page": 999,
            "page_content": "REPLACED",
            "tag": "v1",
        },
    )
    # Must not raise ValueError("'POISON' is not a valid ChunkType").
    chunks = splitter.chunk(doc, partition="p1")
    assert chunks
    for c in chunks:
        assert c.chunk_type == ChunkType.TEXT
        assert c.page_number != 999
        assert c.text != "REPLACED"
        # Other metadata keys still flow through.
        assert c.metadata.get("tag") == "v1"


def test_recursive_splitter_first_block_on_page_three_resolves_correctly():
    """When the first block already starts on page>1, every chunk used to be
    tagged page 1. Now it should land on the actual block page (CodeRabbit #2)."""
    block_text = " ".join([f"word{i}" for i in range(20)])
    splitter = RecursiveSplitter(chunk_size=8, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=block_text, page_number=3),
            TextBlock(text=block_text, page_number=4),
        ],
    )
    chunks = splitter.chunk(doc, partition="p1")
    pages = {c.page_number for c in chunks}
    assert 1 not in pages, f"chunks tagged page 1 despite first block on page 3: {pages}"
    assert any((p or 0) >= 3 for p in pages)


def test_recursive_splitter_skips_pages_get_correct_marker():
    """Block pages 1 -> 5 (skipping 2/3/4) — the second block's chunks must
    resolve to page 5, not page 2 (= last_page+1)."""
    block_text = " ".join([f"word{i}" for i in range(40)])
    splitter = RecursiveSplitter(chunk_size=8, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=block_text, page_number=1),
            TextBlock(text=block_text, page_number=5),
        ],
    )
    chunks = splitter.chunk(doc, partition="p1")
    pages = sorted({c.page_number for c in chunks if c.page_number is not None})
    assert 1 in pages
    assert 5 in pages, f"page 5 missing despite second block on page 5: {pages}"


def test_dewrap_paragraphs_collapses_line_wraps_but_keeps_structure():
    from core.chunking.recursive import dewrap_paragraphs

    text = (
        "# Heading\n"
        "The committee reviewed the report\n"
        "and was assisted by the auditors.\n"
        "\n"
        "- first item\n"
        "- second item\n"
        "[PAGE_2]\n"
    )
    lines = dewrap_paragraphs(text).splitlines()

    # Wrapped prose is joined onto one line.
    assert "The committee reviewed the report and was assisted by the auditors." in lines
    # Markdown block lines keep their own line.
    assert "# Heading" in lines
    assert "- first item" in lines
    assert "- second item" in lines
    assert "[PAGE_2]" in lines


def test_recursive_splitter_does_not_cut_mid_sentence_on_line_wraps():
    """A mid-paragraph line-wrap (as pymupdf4llm emits) must not become a chunk
    boundary; chunks should break on sentence/paragraph boundaries (#579)."""
    # "Alpha beta gamma\ndelta epsilon zeta." carries a wrap inside one sentence.
    text = "Alpha beta gamma\ndelta epsilon zeta. Eta theta iota kappa lambda mu. Nu xi omicron pi rho sigma."
    splitter = RecursiveSplitter(chunk_size=12, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=text, page_number=1)],
        metadata={"source": "wrapped.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert len(chunks) >= 2
    # The wrap word must never end a chunk...
    assert not any(c.text.strip().endswith("gamma") for c in chunks)
    # ...and every text chunk ends on a sentence terminator.
    assert all(c.text.strip()[-1] in ".?!" for c in chunks if c.chunk_type == ChunkType.TEXT)


def test_dewrap_preserves_code_fence_verbatim():
    """Lines inside a ``` fence must keep their indentation/line breaks (not be
    space-joined as prose), while surrounding prose still reflows (#583)."""
    from core.chunking.recursive import dewrap_paragraphs

    text = "Intro wrapped\nacross two lines.\n```python\ndef f():\n    return 1\n```\nOutro wrapped\nacross two.\n"
    lines = dewrap_paragraphs(text).splitlines()

    assert "def f():" in lines  # code line on its own, not joined to the fence
    assert "    return 1" in lines  # indentation preserved
    assert "Intro wrapped across two lines." in lines  # prose still reflowed
    assert "Outro wrapped across two." in lines


def test_placeholder_only_image_block_is_skipped():
    """A caption the VLM could make nothing of is still noise — keep skipping it."""
    from core.chunking.recursive import is_placeholder_image

    assert is_placeholder_image("<image_description>\n\n[Image Placeholder]\n\n</image_description>")
    assert is_placeholder_image("[IMAGE PLACEHOLDER]")
    assert not is_placeholder_image("<image_description>\n\nA bar chart of revenue.\n\n</image_description>")


def test_image_block_keeps_real_caption_beside_a_placeholder():
    """The marker means the captioner found nothing in *one* image, but a block
    can describe several. Dropping the whole block on any occurrence deleted
    real caption text with it — a 132-token chart caption and 225 words of a
    tourism description went missing from the index on the test corpus."""
    from core.chunking.recursive import is_placeholder_image

    # One wrapper holding both. wrap_caption() emits one wrapper per image, so
    # this is the shape the bug actually takes: a composite figure the VLM
    # partly described. Two *adjacent* wrappers were never affected — IMAGE_RE
    # is non-greedy, so they parse as separate elements.
    mixed = (
        "<image_description>\n\n[Image Placeholder]\n\n92 000 COLLABORATEURS\n27 000 AVTOVAZ\n\n</image_description>"
    )
    assert not is_placeholder_image(mixed)


def test_mixed_image_block_survives_chunking():
    splitter = RecursiveSplitter(chunk_size=200, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(
                text=(
                    "Intro paragraph.\n\n"
                    "<image_description>\n\n[Image Placeholder]\n\n"
                    "92 000 COLLABORATEURS 27 000 AVTOVAZ\n\n</image_description>\n\n"
                    "Closing paragraph."
                ),
                page_number=1,
            )
        ],
        metadata={"source": "test.md"},
    )
    joined = "\n".join(c.text for c in splitter.chunk(doc, partition="p"))
    assert "92 000 COLLABORATEURS" in joined, "real caption text dropped alongside the placeholder"
    assert "[Image Placeholder]" not in joined, "the marker reached the index as if it were caption text"


def test_symbol_only_caption_is_not_treated_as_a_placeholder():
    """A caption reduced to punctuation is still content. Testing for
    alphanumerics rather than whitespace would carve out an exception to the
    rule this guard exists to enforce."""
    from core.chunking.recursive import is_placeholder_image

    assert not is_placeholder_image("<image_description>\n\n[Image Placeholder]\n\n©\n\n</image_description>")
    assert is_placeholder_image("<image_description>\n\n[Image Placeholder]\n\n   \n\n</image_description>")


def _standalone_table(rows: int = 8) -> str:
    """An oversized markdown table (>100 tokens) that must stay standalone.

    Each row pads its value column so the whole table exceeds the inline
    threshold; with a small chunk_size the table splits via chunk_table.
    """
    header = "| Group | Value |\n|-------|-------|\n"
    return header + "\n".join(f"| row{i} | {' '.join(['x'] * 30)} |" for i in range(rows))


def test_recursive_splitter_preserves_source_order_around_standalone_table():
    """#353: `intro -> large table -> outro` on one page must emit chunks in
    source order — the table must not jump ahead of the intro, the intro and
    outro prose must not merge across the table boundary, and chunk() must
    number chunk_index in reading order."""
    md = f"Intro paragraph one.\n\n{_standalone_table()}\n\nOutro paragraph two."
    splitter = RecursiveSplitter(chunk_size=40, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=md, page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # The reading order is text(intro) -> table(s) -> text(outro).
    assert chunks[0].chunk_type == ChunkType.TEXT
    assert "Intro paragraph one." in chunks[0].text
    assert chunks[-1].chunk_type == ChunkType.TEXT
    assert "Outro paragraph two." in chunks[-1].text
    assert all(c.chunk_type == ChunkType.TABLE for c in chunks[1:-1])
    # No chunk welds prose from opposite sides of the table.
    for c in chunks:
        assert not ("Intro paragraph" in c.text and "Outro paragraph" in c.text)


def test_recursive_splitter_preserves_source_order_around_standalone_image():
    """#353: an oversized image caption must not reorder content — the prose
    before and after an <image_description> block stays on either side and
    the caption lands between them."""
    long_caption = "lorem ipsum dolor sit amet " * 60  # well above inline threshold
    md = f"Intro paragraph one.\n\n<image_description>\n{long_caption}\n</image_description>\n\nOutro paragraph two."
    splitter = RecursiveSplitter(chunk_size=400, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=md, page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_type == ChunkType.TEXT
    assert "Intro paragraph one." in chunks[0].text
    image_chunks = [c for c in chunks if c.chunk_type == ChunkType.IMAGE_CAPTION]
    assert image_chunks, "expected at least one image_caption chunk"
    assert chunks[-1].chunk_type == ChunkType.TEXT
    assert "Outro paragraph two." in chunks[-1].text
    # No chunk welds prose from opposite sides of the image.
    for c in chunks:
        assert not ("Intro paragraph" in c.text and "Outro paragraph" in c.text)


def test_recursive_splitter_order_and_pages_hold_across_page_flushes():
    """#353: with the final page-sort gone, buffered text flushed between
    standalone elements must still resolve its pages correctly — a markerless
    run following a table inherits the table's end page, and source order is
    preserved even when it disagrees with page order."""
    splitter = RecursiveSplitter(chunk_size=40, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text="Intro paragraph one.", page_number=1),
            TextBlock(text=_standalone_table() + "\n\nBetween the table and the next page.", page_number=2),
            TextBlock(text="Outro paragraph two.", page_number=3),
        ],
        metadata={"source": "multipage.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    table_chunks = [c for c in chunks if c.chunk_type == ChunkType.TABLE]
    assert table_chunks
    # The intro opens the document and the outro closes it, whatever pages they sit on.
    assert chunks[0].chunk_type == ChunkType.TEXT
    assert "Intro paragraph one." in chunks[0].text
    assert chunks[0].page_number == 1
    assert chunks[-1].chunk_type == ChunkType.TEXT
    assert "Outro paragraph two." in chunks[-1].text
    # The table lives on page 2, mid-sequence, never before the intro.
    assert chunks[0].chunk_index < table_chunks[0].chunk_index
    assert table_chunks[-1].chunk_index < chunks[-1].chunk_index
    assert all(c.page_number == 2 for c in table_chunks)
    # The prose following the table has no marker of its own; it must resolve
    # to the table's page, not rewind to the last text chunk's end page.
    between_chunks = [c for c in chunks if c.chunk_type == ChunkType.TEXT and "Between the table" in c.text]
    assert between_chunks and all(c.page_number == 2 for c in between_chunks)


def test_recursive_splitter_small_table_inline_keeps_source_order():
    """A table under the inline threshold still flows through the text path
    (no standalone TABLE chunk) — and its position between the surrounding
    prose survives, inside the chunk the splitter produces."""
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    md = f"Intro paragraph one.\n\n{table}\n\nOutro paragraph two."
    splitter = RecursiveSplitter(chunk_size=400, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=md, page_number=1)],
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert chunks
    assert all(c.chunk_type == ChunkType.TEXT for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    intro_pos = next(i for i, c in enumerate(chunks) if "Intro paragraph one." in c.text)
    table_pos = next(i for i, c in enumerate(chunks) if "| 1 | 2 |" in c.text)
    outro_pos = next(i for i, c in enumerate(chunks) if "Outro paragraph two." in c.text)
    assert intro_pos <= table_pos <= outro_pos


def _strip_page_markers(text: str) -> str:
    """Remove ``[PAGE_N]`` markers and surrounding whitespace from ``text``."""
    import re

    return re.sub(r"\[PAGE_\d+\]", "", text).strip()


def test_page_starting_with_standalone_table_emits_no_marker_only_text_chunk():
    """A document that opens on a table-only page used to emit a text chunk
    holding nothing but the page's ``[PAGE_1]`` marker: the buffer flushed
    ahead of the standalone table contained only the marker. That chunk must
    be dropped — the standalone table keeps its place in order, and the page
    transition must not strand content or rewind page attribution."""
    splitter = RecursiveSplitter(chunk_size=40, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=_standalone_table(), page_number=2),
            TextBlock(text="Outro paragraph two.", page_number=3),
        ],
        metadata={"source": "table-first.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    text_chunks = [c for c in chunks if c.chunk_type == ChunkType.TEXT]
    for c in text_chunks:
        assert _strip_page_markers(c.text), f"text chunk holds nothing but a page marker: {c.text!r}"
    # The standalone table is emitted before the prose that follows it, and
    # the marker-only flush did not push it around.
    table_chunks = [c for c in chunks if c.chunk_type == ChunkType.TABLE]
    assert table_chunks
    assert chunks[0].chunk_type == ChunkType.TABLE
    outro_chunks = [c for c in text_chunks if "Outro paragraph two." in c.text]
    assert len(outro_chunks) == 1, f"outro lost or duplicated: {[c.text for c in text_chunks]}"


def test_consecutive_standalone_pages_emit_no_marker_only_text_chunks():
    """Pages whose entire content is a standalone image description used to
    emit ``[PAGE_1]``/``[PAGE_2]`` text chunks between the image chunks — the
    marker flushed alone as each next standalone element took over (#353
    review). The images must come out alone, one per page, in order."""
    splitter = RecursiveSplitter(chunk_size=200, chunk_overlap_rate=0.0, length_function=_word_tokens)
    caption = "<image_description>\n" + " ".join(["caption"] * 120) + "\n</image_description>"
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=caption, page_number=page) for page in (1, 2, 3)],
        metadata={"source": "images-only.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert [c.page_number for c in chunks] == [1, 2, 3]
    assert all(c.chunk_type == ChunkType.IMAGE_CAPTION for c in chunks), [
        f"{c.chunk_type.name}: {c.text[:40]!r}" for c in chunks
    ]
    # Nothing was lost: every page's caption made it into exactly one chunk.
    chunks_per_page = {page: [c for c in chunks if c.page_number == page] for page in (1, 2, 3)}
    assert all(len(parts) == 1 for parts in chunks_per_page.values())


def test_page_break_isolating_a_trailing_marker_emits_no_marker_only_text_chunk():
    """Prose that fills ``chunk_size`` just before a page break makes the
    splitter emit ``[PAGE_N]`` as a piece of its own: the guard that checked
    the whole flushed buffer froze ``has_prose`` at ``True`` and let that
    marker-only text chunk through. The check must happen per split piece —
    the page state still advances for every piece before one is skipped."""
    prose = " ".join(f"word{i}" for i in range(40))  # 40 tokens, fills chunk_size
    splitter = RecursiveSplitter(chunk_size=40, chunk_overlap_rate=0.0, length_function=_word_tokens)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=prose, page_number=1),
            TextBlock(text=_standalone_table(), page_number=2),
            TextBlock(text="Outro paragraph two.", page_number=3),
        ],
        metadata={"source": "dense-prose-then-table.md"},
    )
    chunks = splitter.chunk(doc, partition="p1")

    assert chunks, "expected chunks to be produced"
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # No text chunk may hold nothing but page markers. This is what the
    # buffer-level guard missed: the marker piece split off a prose-bearing
    # buffer still only carries the marker.
    text_chunks = [c for c in chunks if c.chunk_type == ChunkType.TEXT]
    assert text_chunks
    for c in text_chunks:
        assert _strip_page_markers(c.text), f"text chunk holds nothing but a page marker: {c.text!r}"
    # The prose survives exactly once, on the page it was written on.
    prose_chunks = [c for c in text_chunks if "word0 " in c.text and "word39" in c.text]
    assert len(prose_chunks) == 1, f"prose lost or duplicated: {[c.text[:60] for c in text_chunks]}"
    assert prose_chunks[0].chunk_index == 0
    assert prose_chunks[0].page_number == 1
    # The standalone table follows the prose in source order, on its page.
    table_chunks = [c for c in chunks if c.chunk_type == ChunkType.TABLE]
    assert table_chunks
    assert prose_chunks[0].chunk_index < table_chunks[0].chunk_index
    assert all(c.page_number == 2 for c in table_chunks)
    # The outro's page resolves through the untouched prev_page chain — the
    # skipped marker piece must not have rewound or lost the page state.
    outro_chunks = [c for c in text_chunks if "Outro paragraph two." in c.text]
    assert len(outro_chunks) == 1
    assert outro_chunks[0].page_number == 3
