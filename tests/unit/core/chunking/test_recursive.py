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

    mixed = (
        "<image_description>\n\n[Image Placeholder]\n\n</image_description>\n"
        "<image_description>\n\n92 000 COLLABORATEURS\n27 000 AVTOVAZ\n16 PAYS\n\n</image_description>"
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
                    "<image_description>\n\n[Image Placeholder]\n\n</image_description>\n"
                    "<image_description>\n\n92 000 COLLABORATEURS 27 000 AVTOVAZ\n\n</image_description>\n\n"
                    "Closing paragraph."
                ),
                page_number=1,
            )
        ],
        metadata={"source": "test.md"},
    )
    joined = "\n".join(c.text for c in splitter.chunk(doc, partition="p"))
    assert "92 000 COLLABORATEURS" in joined, "real caption text dropped alongside the placeholder"
