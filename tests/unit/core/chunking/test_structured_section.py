"""Tests for the structure-aware ``structured_section`` chunker."""

from __future__ import annotations

from core.chunking.registry import chunking_registry
from core.chunking.structured_section import StructuredSectionChunker
from core.models.chunk import ChunkType
from core.models.document import ProcessedDocument, TextBlock


def _words(text: str) -> int:
    return len(text.split())


def _chunker(**kw) -> StructuredSectionChunker:
    kw.setdefault("length_function", _words)
    return StructuredSectionChunker(**kw)


def _doc(text: str, page: int = 1, **meta) -> ProcessedDocument:
    return ProcessedDocument(
        document_id="d1",
        text_blocks=[TextBlock(text=text, page_number=page)],
        metadata={"filename": "code.pdf", **meta},
    )


LEGAL = """Livre Ier : Dispositions générales

Titre Ier : Principes

Chapitre unique

Article L110-1
Le présent code régit l'entrée et le séjour des étrangers.

Article L110-2
L'étranger qui séjourne régulièrement peut solliciter une carte.

Titre II : Séjour

Article L120-1
Un étranger peut se voir délivrer une carte de résident valable dix ans.
"""


def test_registered():
    assert "structured_section" in chunking_registry


def test_returns_chunks_with_structure_metadata():
    chunks = _chunker(chunk_size=40).chunk(_doc(LEGAL), partition="p1")
    assert chunks
    assert all(c.partition == "p1" for c in chunks)
    assert all(c.document_id == "d1" for c in chunks)
    # Every chunk is self-describing: a heading-path breadcrumb in the header
    # and in metadata.
    assert all(c.header and c.header.startswith("[") for c in chunks)
    assert all("hierarchy_path" in c.metadata for c in chunks)
    assert any(c.metadata["hierarchy_path"] for c in chunks)


def test_no_orphan_heading_chunks():
    # A bare heading (Titre/Chapitre) must never be emitted as its own chunk —
    # it only sets context for the leaves beneath it.
    chunks = _chunker(chunk_size=40).chunk(_doc(LEGAL))
    for c in chunks:
        body_lines = [ln for ln in (c.content or "").splitlines() if ln.strip()]
        assert body_lines, "chunk body should never be empty"
        assert not body_lines[0].startswith("Titre")
        assert not body_lines[0].startswith("Chapitre")


def _is_bare_marker(line: str) -> bool:
    """An ``Article`` marker line with no body of its own (<=2 trailing words)."""
    line = line.strip()
    return line.startswith("Article") and len(line.split()) <= 3


def test_articles_kept_whole_and_packed():
    # Short articles under one heading pack together; none is torn (no chunk
    # ends on a *bare* article marker whose body spilled into the next chunk).
    chunks = _chunker(chunk_size=60).chunk(_doc(LEGAL))
    for c in chunks:
        last = (c.content or "").strip().splitlines()[-1]
        assert not _is_bare_marker(last), "article marker stranded at chunk end"
    joined = "\n".join(c.content or "" for c in chunks)
    for marker in ("Article L110-1", "Article L110-2", "Article L120-1"):
        assert joined.count(marker) == 1, f"{marker} duplicated or dropped"


def test_heading_path_tracks_hierarchy():
    chunks = _chunker(chunk_size=30).chunk(_doc(LEGAL))
    # The L120-1 article sits under Titre II, not Titre I.
    l120 = next(c for c in chunks if "Article L120-1" in (c.content or ""))
    assert "Titre II : Séjour" in l120.metadata["hierarchy_path"]
    assert "Titre Ier : Principes" not in l120.metadata["hierarchy_path"]


def test_zero_overlap_no_duplication():
    chunks = _chunker(chunk_size=30).chunk(_doc(LEGAL))
    bodies = [(c.content or "") for c in chunks]
    # No article body text is replayed across adjacent chunks.
    for a, b in zip(bodies, bodies[1:]):
        tail = " ".join(a.split()[-5:])
        assert tail not in b


def test_page_markers_become_metadata_not_text():
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text="Article L1\nBody on page one here.", page_number=1),
            TextBlock(text="Article L2\nBody on page two here.", page_number=2),
        ],
        metadata={"filename": "code.pdf"},
    )
    chunks = _chunker(chunk_size=30).chunk(doc)
    assert chunks
    for c in chunks:
        assert "[PAGE_" not in c.text
        assert c.page_number in (1, 2)


def test_large_table_is_atomic_table_chunk():
    table = "| Domain | Value |\n|---|---|\n" + "\n".join(f"| D{i} | {' '.join(['x'] * 40)} |" for i in range(8))
    doc = _doc(f"Article L1\nSome intro prose.\n\n{table}\n\nArticle L2\nMore prose after the table.")
    chunks = _chunker(chunk_size=50).chunk(doc)
    assert any(c.chunk_type == ChunkType.TABLE for c in chunks), "expected an atomic table chunk"


def test_oversize_article_is_split_not_dropped():
    big_body = " ".join(f"clause{i}." for i in range(200))
    doc = _doc(f"Article L1\n{big_body}")
    chunks = _chunker(chunk_size=30, max_tokens=45).chunk(doc)
    assert len(chunks) > 1, "an over-max article must be split"
    assert all(c.chunk_type == ChunkType.TEXT for c in chunks)
    # All split pieces keep the same section path.
    paths = {tuple(c.metadata["hierarchy_path"]) for c in chunks}
    assert len(paths) == 1


def test_markdown_headings_also_detected():
    # Bodies are kept above min_tokens so the two sibling chapters don't pack
    # together — this isolates heading *detection*, not sibling merging.
    body_a = " ".join(["alpha"] * 20)
    body_b = " ".join(["beta"] * 20)
    md = f"# Book One\n\n## Chapter A\n\nArticle L1\n{body_a}\n\n## Chapter B\n\nArticle L2\n{body_b}"
    chunks = _chunker(chunk_size=25).chunk(_doc(md))
    l2 = next(c for c in chunks if "Article L2" in (c.content or ""))
    assert "Chapter B" in l2.metadata["hierarchy_path"]
    assert "Chapter A" not in l2.metadata["hierarchy_path"]


def test_empty_document_returns_no_chunks():
    assert _chunker().chunk(ProcessedDocument(document_id="d1", text_blocks=[])) == []
    assert _chunker().chunk(_doc("   \n  \n")) == []
