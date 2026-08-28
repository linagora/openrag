"""Tests for the structure-aware ``structured_section`` chunker."""

from __future__ import annotations

import re

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


def test_registered_via_package_import_only():
    # Production never imports ``core.chunking.structured_section`` directly: the
    # factory and the admin preset options both go through ``core.chunking`` and
    # rely on the strategy self-registering when the package is imported. This
    # test file imports the module at the top (registering it process-wide), so
    # it can't observe the gap — a clean subprocess that imports ONLY the package
    # can. Regression: the strategy was registered nowhere in the import graph,
    # so ``create_chunker('structured_section')`` raised and the admin UI never
    # listed it.
    import os
    import subprocess
    import sys

    import core

    src = os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))
    code = (
        "from core.chunking import chunking_registry\n"
        "assert 'structured_section' in chunking_registry, sorted(chunking_registry.list_registered())\n"
    )
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"structured_section not registered via package import: {r.stderr}"


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
    # Heading lines live in the body (so they reach the index at all), so a
    # chunk may legitimately *open* with its heading — what must never happen
    # is a chunk that is nothing but headings, carrying no content of its own.
    chunks = _chunker(chunk_size=40).chunk(_doc(LEGAL))
    for c in chunks:
        body_lines = [ln for ln in (c.content or "").splitlines() if ln.strip()]
        assert body_lines, "chunk body should never be empty"
        headings = [ln for ln in body_lines if ln.startswith(("Livre", "Titre", "Chapitre"))]
        assert len(headings) < len(body_lines), f"chunk is only headings: {body_lines}"


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


def test_caption_over_max_tokens_is_left_whole():
    # ``max_tokens`` is the *packing* ceiling for prose; an atomic caption is
    # exempt. Splitting one strands the figure it describes — measured on the
    # marker corpus it fragmented 55 captions for no embedding benefit.
    caption = " ".join(f"sentence{i} describing the figure." for i in range(120))
    doc = _doc(f"Article L1\nIntro prose.\n\n<image_description>{caption}</image_description>\n")
    captions = [c for c in _chunker(chunk_size=30, max_tokens=45).chunk(doc) if c.chunk_type == ChunkType.IMAGE_CAPTION]
    assert len(captions) == 1, "a caption must not be split by the packing ceiling"
    assert _words(captions[0].text) > 45


def test_caption_over_hard_max_tokens_is_split():
    # The hard bound is the embedder's window, not the packing target: past it
    # content would be silently truncated, so a pathological caption is cut.
    caption = " ".join(f"sentence{i} describing the figure." for i in range(120))
    doc = _doc(f"Article L1\nIntro prose.\n\n<image_description>{caption}</image_description>\n")
    chunks = _chunker(chunk_size=30, max_tokens=45, hard_max_tokens=200).chunk(doc)
    captions = [c for c in chunks if c.chunk_type == ChunkType.IMAGE_CAPTION]
    assert len(captions) > 1, "a caption past the hard bound must be split"
    assert all(_words(c.text) <= 200 for c in captions)


def test_hard_max_tokens_cuts_as_few_times_as_possible():
    # Splitting back down to ``chunk_size`` would shred a tripped caption; the
    # net packs to the bound itself instead.
    caption = " ".join(f"sentence{i} describing the figure." for i in range(120))
    doc = _doc(f"Article L1\nIntro prose.\n\n<image_description>{caption}</image_description>\n")
    captions = [
        c
        for c in _chunker(chunk_size=30, max_tokens=45, hard_max_tokens=300).chunk(doc)
        if c.chunk_type == ChunkType.IMAGE_CAPTION
    ]
    assert len(captions) <= 3, f"expected a minimal number of cuts, got {len(captions)}"


def test_split_caption_never_ends_on_its_own_heading():
    # VLM captions carry their own ``###`` sub-headings, so packing paragraphs
    # could strand one at a piece's end, away from the body it introduces.
    body = " ".join(f"detail{i} about the chart." for i in range(60))
    caption = f"{body}\n\n### Key Trends\n\n{body}\n\n### Conclusions\n\n{body}"
    doc = _doc(f"Article L1\nIntro.\n\n<image_description>{caption}</image_description>\n")
    chunks = _chunker(chunk_size=40, max_tokens=60, hard_max_tokens=120).chunk(doc)
    captions = [c for c in chunks if c.chunk_type == ChunkType.IMAGE_CAPTION]
    assert len(captions) > 1, "expected the caption to be split"
    for chunk in captions:
        last = [line for line in chunk.text.splitlines() if line.strip()][-1]
        assert not last.lstrip().startswith("#"), f"heading stranded at chunk end: {last!r}"


def test_breakless_paragraph_splits_on_clauses_not_mid_sentence():
    # A long recital with no '.' — only semicolons/commas. Word-wrapping was the
    # only fallback, so pieces landed mid-sentence; the clause ladder splits on
    # the punctuation that is actually there.
    clauses = "; ".join(f"considerant que le point {i} demeure applicable" for i in range(40))
    doc = _doc(f"Article L1\n{clauses}")
    chunks = _chunker(chunk_size=30, max_tokens=45).chunk(doc)
    assert len(chunks) > 1, "the recital must be split"
    for chunk in chunks:
        body = (chunk.content or chunk.text).strip()
        body = body.split("]", 1)[-1].strip() if body.startswith("[Source") else body
        assert body.startswith(("considerant", "Article")), f"piece opens mid-clause: {body[:60]!r}"


def test_small_caption_does_not_break_the_merge_chain():
    # Slide shape: heading, one-line label, tiny caption, one-line label. The
    # atomic caption used to sit between the two under-min labels and block
    # merging, leaving three tiny chunks per slide.
    md = "## Slide One\n\nSales by brand\n\n<image_description>RG</image_description>\n\nIn percent\n"
    chunks = _chunker(chunk_size=60).chunk(_doc(md))
    assert len(chunks) == 1, f"slide should collapse into one chunk, got {len(chunks)}"
    assert "Sales by brand" in chunks[0].text
    assert "In percent" in chunks[0].text


def test_same_page_slide_collapses_across_atomic_types():
    # A slide is page-atomic: label + table + label belong in one chunk. The
    # structural route refuses tables, so only the same-page route can do this.
    rows = "\n".join(f"| D{i} | {' '.join(['x'] * 12)} |" for i in range(8))
    md = f"## Slide One\n\nSales by brand\n\n| Domain | Value |\n|---|---|\n{rows}\n\nIn percent\n"
    chunks = _chunker(chunk_size=512).chunk(_doc(md, page=3))
    assert len(chunks) == 1, f"one slide should be one chunk, got {len(chunks)}"
    assert "Sales by brand" in chunks[0].text
    assert "In percent" in chunks[0].text


def test_headingless_unit_does_not_chain_unrelated_sections():
    # ``_compatible([], X)`` used to be True, so a path-less cover block could
    # absorb content from an unrelated deep section on another page.
    body = " ".join(["alpha"] * 60)
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text="COVER PAGE 2025", page_number=1),
            TextBlock(text=f"# Book One\n\n## Chapter A\n\n{body}", page_number=9),
        ],
        metadata={"filename": "code.pdf"},
    )
    chunks = _chunker(chunk_size=512).chunk(doc)
    cover = next(c for c in chunks if "COVER PAGE 2025" in c.text)
    assert "alpha" not in cover.text, "cover block absorbed an unrelated section"


def test_table_does_not_absorb_prose_from_another_page():
    # Off-page, only the structural route applies and it excludes tables, so a
    # stray label cannot be glued to a table it does not belong to. (On the same
    # page it may — see test_same_page_slide_collapses_across_atomic_types.)
    rows = "\n".join(f"| D{i} | {' '.join(['x'] * 12)} |" for i in range(8))
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=f"## Slide One\n\n| Domain | Value |\n|---|---|\n{rows}\n", page_number=2),
            TextBlock(text="## Slide One\n\nStray label", page_number=7),
        ],
        metadata={"filename": "code.pdf"},
    )
    tables = [c for c in _chunker(chunk_size=512).chunk(doc) if c.chunk_type == ChunkType.TABLE]
    assert tables, "the table must stay its own chunk(s)"
    for table in tables:
        assert "Stray label" not in table.text, "table absorbed prose from another page"


def test_oversize_table_row_is_never_sentence_split():
    # Tables are exempt from ``_enforce_ceiling``: a row over the ceiling is
    # indivisible, and slicing it mid-sentence yields a fragment that is neither
    # a valid row nor carries its column headers. Measured on the marker corpus,
    # a row-level fallback doubled total excess tokens — see _enforce_ceiling.
    doc = _doc(f"Article L1\nIntro.\n\n| Ref | Text |\n|---|---|\n| R1 | {' '.join(['word'] * 200)} |\n")
    tables = [c for c in _chunker(chunk_size=30, max_tokens=45).chunk(doc) if c.chunk_type == ChunkType.TABLE]
    assert len(tables) == 1
    assert _words(tables[0].text) > 45, "the row is kept whole rather than mangled"


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


def test_flat_markdown_hierarchy_keeps_all_ancestors():
    # Marker emits an entire legal hierarchy at a single ``#`` level. Taking the
    # flat markdown depth would let these same-level headings pop one another off
    # the stack, collapsing the breadcrumb to just the innermost heading and
    # dropping every ancestor — including the document title. Structural keywords
    # must re-establish the nesting so the whole path survives.
    md = (
        "# Code de l'entrée et du séjour\n\n"
        "# Partie législative\n\n"
        "# Livre I : Dispositions générales\n\n"
        "# Titre I : Champ d'application\n\n"
        "Article L110-1\n"
        "Le présent code régit l'entrée et le séjour des étrangers en France.\n"
    )
    chunk = _chunker(chunk_size=40).chunk(_doc(md))[0]
    path = chunk.metadata["hierarchy_path"]
    assert path == [
        "Code de l'entrée et du séjour",
        "Partie législative",
        "Livre I : Dispositions générales",
        "Titre I : Champ d'application",
    ]


def test_escaped_emphasis_marker_is_a_leaf_not_a_heading():
    # Marker escapes emphasis inside a marker (``Article R\*352-1*`` for an
    # italic ``R*352-1*``). The stray backslash must not push the marker off the
    # leaf test and into the heading stack, or it pollutes the breadcrumb of
    # every chunk beneath it.
    md = (
        "# Livre I : Dispositions\n\n"
        r"Article R\*352-1*"
        "\nL'autorité administrative statue sur la demande dans les conditions prévues.\n"
    )
    chunk = _chunker(chunk_size=40).chunk(_doc(md))[0]
    assert chunk.metadata["hierarchy_path"] == ["Livre I : Dispositions"]
    assert not any("352-1" in h for h in chunk.metadata["hierarchy_path"])


def test_non_structural_headings_stay_out_of_breadcrumb():
    # Parsers mark captions, TOC leaders, image credits, bare page numbers,
    # amendment enumerations, and stray sentences as `#` headings. None should
    # become a section label; the real heading must remain the breadcrumb, and
    # the rejected line stays as body content.
    body = " ".join(["lorem"] * 30)
    md = (
        "## Real Section\n\n"
        "# Figure 2 - Examples of computing services\n\n"
        "# SUMMARY INTRODUCTION .......... 17\n\n"
        "# photo © oticki - stock.adobe.com\n\n"
        "# 1\n\n"
        "# 11° L'article L. 413-3 est ainsi rédigé :\n\n"
        "# The strategy implemented since 2018 has produced measurable and lasting national results\n\n"
        f"Article L1\n{body}\n"
    )
    chunks = _chunker(chunk_size=60).chunk(_doc(md))
    l1 = next(c for c in chunks if "Article L1" in (c.content or ""))
    assert l1.metadata["hierarchy_path"] == ["Real Section"]
    # None of the rejected lines became a section label anywhere.
    labels = {h for c in chunks for h in c.metadata["hierarchy_path"]}
    assert labels == {"Real Section"}
    # The rejected lines are preserved as body somewhere, not lost.
    alltext = "\n".join(c.text for c in chunks)
    assert "Figure 2" in alltext
    assert "The strategy implemented since 2018" in alltext


def test_keyword_sentence_is_not_taken_as_heading():
    # A body sentence opening with a structural keyword (common in pymupdf output,
    # which emits no `#` headings) must not be mistaken for a `Partie` heading.
    md = "# Livre I : Dispositions\n\npartie de ce total doit être expliquée par la hausse observée cette année.\n"
    chunk = _chunker(chunk_size=60).chunk(_doc(md))[0]
    assert chunk.metadata["hierarchy_path"] == ["Livre I : Dispositions"]


def test_annexe_heading_is_structural():
    md = (
        "# Annexe 1 - Suivi de la mise en œuvre des propositions pour 2024 et années suivantes\n\n"
        "Article L1\nLe contenu de l'annexe figure ci-après pour information du lecteur.\n"
    )
    chunk = _chunker(chunk_size=60).chunk(_doc(md))[0]
    assert any("Annexe 1" in h for h in chunk.metadata["hierarchy_path"])


def test_span_anchor_stripped_from_heading():
    md = '# <span id="page-46-0"></span>Dispositions générales\n\nArticle L1\nLe présent article régit les conditions applicables.\n'
    chunk = _chunker(chunk_size=60).chunk(_doc(md))[0]
    assert chunk.metadata["hierarchy_path"] == ["Dispositions générales"]


def test_empty_document_returns_no_chunks():
    assert _chunker().chunk(ProcessedDocument(document_id="d1", text_blocks=[])) == []
    assert _chunker().chunk(_doc("   \n  \n")) == []


def test_hard_max_tokens_derived_from_embedder_window():
    # The bound tracks the embedder the partition actually uses, not chunk_size:
    # a partition on a small-context embedder must get a tighter bound.
    from types import SimpleNamespace

    from core.chunking.factory import resolve_hard_max_tokens

    cfg = SimpleNamespace(hard_max_tokens=None)
    assert resolve_hard_max_tokens(cfg, 2047) == 1023
    assert resolve_hard_max_tokens(cfg, 8192) == 4096
    # No known window => no invented bound (chunk_size says nothing about it).
    assert resolve_hard_max_tokens(cfg, None) is None
    # An explicit setting always wins.
    assert resolve_hard_max_tokens(SimpleNamespace(hard_max_tokens=777), 8192) == 777


def test_hard_wrap_respects_target_for_token_dense_words():
    """Word count only estimates token count. A slice of token-dense words
    (URLs, long identifiers) tokenises far above the average, so slicing purely
    on ceil(tokens/target) could emit a piece over the ceiling."""
    from core.chunking.structured_section import _hard_wrap

    # 4 tokens per "word", so a word-uniform slice overshoots a token budget.
    def dense(text: str) -> int:
        return len(text.split()) * 4

    words = " ".join(f"w{i}" for i in range(40))
    for piece in _hard_wrap(words, 20, dense):
        assert dense(piece) <= 20, f"piece over target: {dense(piece)}"


def test_hard_wrap_emits_an_oversize_single_word_alone():
    from core.chunking.structured_section import _hard_wrap

    pieces = _hard_wrap("short " + "x" * 400, 5, _words)
    assert pieces, "must not drop the text"
    assert any("x" * 400 in p for p in pieces), "the long word survives intact"


def test_invalid_leaf_pattern_fails_at_config_load():
    import pytest
    from core.config.chunking import ChunkerConfig

    ChunkerConfig(leaf_patterns=[r"^\s*Article\s+\d"])  # valid: no raise
    with pytest.raises(ValueError, match="invalid leaf_patterns regex"):
        ChunkerConfig(leaf_patterns=["^(unclosed"])


def test_non_positive_token_bounds_fail_at_config_load():
    import pytest
    from core.config.chunking import ChunkerConfig

    for field in ("min_tokens", "max_tokens", "hard_max_tokens"):
        with pytest.raises(ValueError):
            ChunkerConfig(**{field: 0})
        with pytest.raises(ValueError):
            ChunkerConfig(**{field: -1})


def test_hard_max_survives_the_small_merge_pass():
    """_pack enforced the bound and _merge_small immediately undid it: the split
    pieces land under min_tokens, share a page, take the permissive same-page
    route, and fold back under the (larger) max_tokens ceiling. The band must
    collapse under the bound so every downstream budget respects it."""
    caption = " ".join(f"word{i}" for i in range(300))
    doc = _doc(f"Article L1\nIntro.\n\n<image_description>{caption}</image_description>\n")
    chunks = _chunker(chunk_size=512, hard_max_tokens=60).chunk(doc)
    assert len(chunks) > 1, "the caption must stay split"
    assert all(c.token_count <= 60 for c in chunks), [c.token_count for c in chunks]


def test_hard_max_bounds_prose_not_just_atomic_units():
    """_enforce_ceiling only runs on atomic units, so prose was bounded solely
    by max_tokens (derived from chunk_size) and could overflow a small
    embedder window untouched."""
    prose = " ".join(f"clause{i}." for i in range(600))
    chunks = _chunker(chunk_size=512, hard_max_tokens=60).chunk(_doc(f"Article L1\n{prose}"))
    assert all(c.token_count <= 60 for c in chunks), max(c.token_count for c in chunks)


def test_hard_max_above_the_band_changes_nothing():
    """The normal case: a 2047-token window gives a 1023 bound, well above the
    768 packing ceiling, so the band is untouched."""
    prose = " ".join(f"clause{i}." for i in range(600))
    doc = _doc(f"Article L1\n{prose}")
    with_bound = _chunker(chunk_size=512, hard_max_tokens=1023).chunk(doc)
    without = _chunker(chunk_size=512).chunk(doc)
    assert [c.token_count for c in with_bound] == [c.token_count for c in without]


def test_header_is_none_when_not_prepended():
    """``header`` is persisted next to ``text``; claiming one the chunk does not
    carry lets a consumer rebuild a string that was never embedded."""
    doc = _doc("# Titre I\n\nArticle L1\nSome body text here.")
    chunk = _chunker(chunk_size=512, prepend_heading_path=False).chunk(doc)[0]
    assert chunk.header is None
    assert not chunk.text.startswith("[Source")


_MULTI_SECTION_PAGE = """Titre Ier : Dispositions fiscales

Chapitre Ier : Taux

Article L110-1
Le taux normal de la taxe sur la valeur ajoutée est fixé à 20 %.

Titre II : Dispositions sanitaires

Chapitre Ier : Vaccination

Article L210-1
La vaccination antidiphtérique est obligatoire pour les mineurs.

Titre III : Dispositions relatives au travail

Article L310-1
La durée légale du travail effectif est fixée à trente-cinq heures.
"""


def test_same_page_merge_does_not_span_unrelated_sections():
    """A slide is a section, so merging on a shared page is safe there. Dense
    text puts several short units per page under different headings — merging
    on page alone produced one chunk holding a tax, health and labour article
    with hierarchy_path=[], erasing every Titre from the header AND metadata.
    """
    chunks = _chunker(chunk_size=512).chunk(_doc(_MULTI_SECTION_PAGE))
    assert len(chunks) > 1, "articles from different Titres must not merge"
    for chunk in chunks:
        assert chunk.metadata["hierarchy_path"], f"breadcrumb erased: {chunk.text[:60]!r}"
    titres = {c.metadata["hierarchy_path"][0] for c in chunks}
    assert len(titres) == 3, f"expected one chunk per Titre, got {titres}"


def test_merge_never_leaves_a_chunk_without_a_breadcrumb():
    """Independent of the route taken: if either side had a path, the merged
    unit keeps one. Losing it entirely puts the heading beyond both dense and
    sparse retrieval, since BM25 is declared over ``text`` alone."""
    md = "## Chapter A\n\nShort intro.\n\nSome trailing note.\n"
    for chunk in _chunker(chunk_size=512).chunk(_doc(md)):
        assert chunk.metadata["hierarchy_path"], f"no breadcrumb on {chunk.text[:60]!r}"


def test_headings_reach_the_index_even_without_the_header():
    """_build_units never emits a heading as body, so the header is the only
    path by which heading text reaches ``text``. Turning the flag off used to
    delete the document's structure from the index outright."""
    doc = _doc("# Livre Ier\n\n## Titre Ier\n\nArticle L110-1\nLe present code regit le sejour.")
    joined = "\n".join(c.text for c in _chunker(chunk_size=512, prepend_heading_path=False).chunk(doc))
    for heading in ("Livre Ier", "Titre Ier"):
        assert heading in joined, f"{heading!r} absent from every chunk's text"
    assert "[Source:" not in joined, "the verbose preamble should still be dropped"


def test_split_pieces_report_the_pages_they_actually_cover():
    """Copying the parent's page set into every piece made page_number the
    unit's first page for all of them and page_range its full span — the
    citation UI then points at page 1 of a four-page document."""
    doc = ProcessedDocument(
        document_id="d1",
        text_blocks=[
            TextBlock(text=" ".join(f"Page {p} sentence {i} about policy." for i in range(40)), page_number=p)
            for p in (1, 2, 3, 4)
        ],
        metadata={"filename": "code.pdf"},
    )
    chunks = _chunker(chunk_size=120).chunk(doc)
    assert len(chunks) > 1, "a four-page unit must split"
    for chunk in chunks:
        body = chunk.content or ""
        opening_page = int(body.split()[1])  # "Page N sentence ..."
        assert chunk.page_number == opening_page, f"page={chunk.page_number} for text on page {opening_page}"
        assert chunk.metadata["page_range"] == str(opening_page)


def test_paragraph_breaks_survive_into_the_body():
    """Blank lines were dropped and the body joined with single newlines, so
    _greedy_split's paragraph rung never matched: every oversize unit fell
    through to the sentence ladder, which rejoins with ' ' and welds headings
    into the middle of prose lines."""
    src = "\n\n".join(
        ["Intro sentence here."]
        + [f"**Section {i}**\n\nBody {i} " + " ".join(f"filler{j}." for j in range(60)) for i in range(4)]
    )
    chunks = _chunker(chunk_size=120).chunk(_doc(src))
    joined = "\n".join(c.content or "" for c in chunks)
    assert "\n\n" in joined, "paragraph breaks erased"
    # Same-line only: ".\n\n**Section**" is the correct shape, ". **Section**" is not.
    welded = re.findall(r"\.[ \t]+\*\*[^*]+\*\*", joined)
    assert not welded, f"headings welded into prose lines: {welded[:3]}"


def test_heading_markdown_survives_into_the_body():
    """The body copy of a heading keeps its ``#`` markers: the depth is the
    cheapest signal of nesting, the chunk is markdown-rendered downstream, and
    recursive_splitter keeps heading lines verbatim — a flattened copy would
    silently degrade every chunk of a partition that switched."""
    doc = _doc(
        "### List of Figures\n\nFigure 1.1: Approaches ... 10\n\n### Acknowledgements\n\nThe authors thank the policymakers."
    )
    body = "\n".join(c.content or "" for c in _chunker(chunk_size=512).chunk(doc))
    assert "### List of Figures" in body
    assert "### Acknowledgements" in body


def test_heading_body_copy_drops_parser_html_anchors():
    """Marker leaves ``<span id="page-46-0">`` anchors in heading lines; those
    are parser noise, unlike the markdown itself."""
    doc = _doc('## <span id="page-5-0"></span>Corporate Responsibility\n\nWe have chosen to act.')
    body = "\n".join(c.content or "" for c in _chunker(chunk_size=512).chunk(doc))
    assert "## Corporate Responsibility" in body
    assert "<span" not in body


def test_image_placeholder_marker_never_reaches_the_chunk_text():
    caption = "[Image Placeholder]\n\n92 000 COLLABORATEURS 27 000 AVTOVAZ " + " ".join(f"detail{i}" for i in range(60))
    doc = _doc(f"Article L1\nIntro.\n\n<image_description>\n\n{caption}\n\n</image_description>\n")
    joined = "\n".join(c.text for c in _chunker(chunk_size=512).chunk(doc))
    assert "92 000 COLLABORATEURS" in joined
    assert "[Image Placeholder]" not in joined
