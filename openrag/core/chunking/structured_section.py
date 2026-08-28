"""Structure-aware chunking strategy (``structured_section``).

Where ``recursive_splitter`` cuts on character separators (paragraph → sentence
→ line → word) with a fixed overlap, ``structured_section`` cuts on the
document's own structure: it detects headings and leaf units (e.g. legal
``Article`` markers) by **regex on line content**, keeps each leaf atomic,
greedily packs consecutive leaves up to a token target, and prepends each chunk
with its heading path so the chunk is self-describing for retrieval.

Why regex-on-lines rather than markdown ``#`` parsing: the default PDF parser
(Marker) emits legal/structural headings (``Titre``, ``Chapitre``,
``Article L110-1`` …) as **plain-text lines, not** ``##`` headings, so a
markdown-``#``-only detector would miss every boundary. Boundaries are
config-driven — ``heading_keywords`` holds literal keywords (escaped by the
constructor), ``leaf_patterns`` holds regexes — so the strategy generalizes
beyond French legal codes; markdown ``#`` headings are recognized too.

Design (in priority order), matching the redesign brief:
1. Boundary detection on line content: markdown ``#``; keyword headings
   (Livre/Titre/Chapitre/Section/…); leaf markers (``Article L###`` …).
2. Atomicity: never split a leaf/section unless it alone exceeds ``max_tokens``;
   then fall back to paragraph → sentence.
3. Packing: greedily pack consecutive short leaves under the same heading up to
   ``target_tokens`` (a chunk = one or a few whole articles under one heading).
4. Self-containment: the heading path is prepended as a header.
5. Overlap = 0 (leaves are atomic; overlap would just duplicate whole articles).
6. ``[PAGE_N]`` markers become ``page_number`` / ``page_range`` metadata, never
   inline text.

Pure domain logic — no LLM, no Ray. The token counter is injected. Tables and
image-description blocks are treated as atomic units via the shared
``markdown_utils`` primitives, exactly like ``recursive.py``.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.chunking.markdown_utils import (
    PAGE_RE,
    MDElement,
    chunk_table,
    split_md_elements,
)
from core.chunking.recursive import (
    _INLINE_ELEMENT_TOKEN_THRESHOLD,
    BaseChunker,
    dewrap_paragraphs,
    is_placeholder_image,
    strip_placeholder_markers,
)
from core.chunking.registry import chunking_registry
from core.models.chunk import Chunk, ChunkType
from core.models.document import ProcessedDocument
from core.utils.text import sanitize_text

# Markdown ATX heading: ``## Title``. Level = number of ``#``.
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*$")

# A leading markdown heading prefix (``#### ``) and inline emphasis markers
# (``**bold**``, ``_em_``, `` `code` ``) — stripped when testing a line for a
# leaf pattern and when cleaning heading text for the breadcrumb.
_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s*")
# Markdown emphasis/code markers plus the escape backslash. The backslash matters
# for leaf detection: Marker escapes emphasis inside markers (``Article R\*352-1*``
# for an italic ``R*352-1*``), and a stray ``\`` sitting where the leaf regex
# expects a digit makes the marker miss the leaf test and get misfiled as a
# heading — polluting the breadcrumb of every chunk beneath it.
_EMPHASIS_RE = re.compile(r"[*_`\\]+")
# Stricter than ``_HEADING_PREFIX_RE``: requires text after the hashes, so a
# ``#hashtag`` line is not mistaken for a heading when re-homing split pieces.
_CAPTION_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
# Inline HTML the parsers leave in heading lines — chiefly Marker's page anchors
# (``<span id="page-46-0"></span>Encadré 3 …``), which would otherwise ride into
# the breadcrumb verbatim.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Caption / credit / TOC-leader prefixes: lines a parser marks as a heading that
# are really figure/table captions or a table-of-contents entry, not a section.
_CAPTION_RE = re.compile(
    r"^(?:figure|fig\.|table|tableau|source|photo|graph(?:e|ique)?|sch[eé]ma|cr[eé]dits?)\b",
    re.IGNORECASE,
)


def _clean_heading(text: str) -> str:
    """Strip HTML, markdown emphasis, and heading noise from a heading's text."""
    text = _HTML_TAG_RE.sub("", text)
    return _EMPHASIS_RE.sub("", _HEADING_PREFIX_RE.sub("", text)).strip()


def _looks_like_heading(text: str, *, keyword_led: bool) -> bool:
    """True if ``text`` is plausibly a *structural* heading.

    Both parsers routinely mark non-headings as headings — figure/table
    captions, table-of-contents leaders, image credits, enumerated amendment
    items (``11° L'article … est ainsi rédigé :``), bare page numbers (pymupdf's
    ``## 1``), table cells, running-header fragments, and whole body sentences.
    Taking any of those as a section pollutes the breadcrumb of every chunk
    beneath it, so reject them here; the line then stays as body text (its
    content is preserved, it just no longer names a section).

    ``keyword_led`` marks a line opening with a structural keyword
    (Partie/Livre/Titre/…). Those are trusted to be real headings even when long
    (legal titles run long) — but must still clear the punctuation/caption gate,
    since a body sentence can open with a keyword word (``Partie de … augmente,``)
    and would otherwise be mistaken for a ``Partie`` heading.
    """
    t = text.strip()
    if not t:
        return False
    # Prose / enumeration punctuation — headings don't end this way.
    if t[-1] in ",;:" or t.endswith(("...", "…")):
        return False
    if t.endswith(".") and len(t.split()) > 3:
        return False
    # Captions, credits, references, TOC dot-leaders, numbered-amendment items.
    if _CAPTION_RE.match(t) or "©" in t or "...." in t or "doi:" in t.lower():
        return False
    if re.match(r"^\d+°", t):
        return False
    # Bare numbers / symbol runs (page numbers, table cells like "4,51 4,79").
    if sum(c.isalpha() for c in t) < 3:
        return False
    if keyword_led:
        return True
    words = t.split()
    if len(words) > 10:  # an 11+-word "heading" is a sentence
        return False
    if t[:1].islower() and len(words) >= 3:  # opens mid-sentence
        return False
    return True


# Keyword headings emitted as plain text by structure-unaware parsers. Ordered
# outermost → innermost; the index gives the nesting level so a heading stack
# can pop correctly. Config can replace this list.
_DEFAULT_HEADING_KEYWORDS: tuple[str, ...] = (
    "partie",
    "annexe",
    "livre",
    "titre",
    "chapitre",
    "section",
    "sous-section",
    "paragraphe",
)

# Leaf markers — a unit that is atomic and does NOT open a nesting level (an
# article sits *inside* the current heading, it doesn't contain sub-articles).
_DEFAULT_LEAF_PATTERNS: tuple[str, ...] = (r"^\s*Art(?:icle|\.)\s*[LRDOlrdo]?\.?\s*\d",)

# A markdown heading counts as its own nesting level (by ``#`` depth); keyword
# headings get a level *below* every markdown heading so a plain-text ``Titre``
# nested under a real ``# Book`` heading still stacks sensibly.
# --- paginated-layout detection -------------------------------------------
# A page in a deck *is* a section, so the page is the right chunk boundary
# there and the wrong one everywhere else. Calibrated on the 30-document
# marker corpus: the two decks sit at 86 and 190 median tokens/page with
# 83%/87% of pages under _SHORT_PAGE, and the next document up is 276 / 77%,
# then a cliff to 364 / 53%. Deliberately conservative — a false "paginated"
# on a long report would chunk it per page, which is much worse than not
# firing on a real deck.
_SHORT_PAGE_TOKENS = 400
_MAX_MEDIAN_TOKENS_PER_PAGE = 300
_MIN_SHORT_PAGE_FRACTION = 0.7
_MIN_PAGES_FOR_DETECTION = 8

_MD_LEVEL_BASE = 0
_KEYWORD_LEVEL_BASE = 100


@dataclass
class _Heading:
    """A heading on the open stack. ``used`` records whether any unit was
    flushed beneath it, i.e. whether its text ever reached a chunk."""

    level: int
    text: str
    used: bool = False


@dataclass
class _Unit:
    """A structural unit: a leaf (article/heading-intro) or an atomic block."""

    heading_path: list[str]
    text: str
    tokens: int
    pages: set[int] = field(default_factory=set)
    #: ``(character offset in ``text``, page)`` for each line, so a piece of a
    #: split unit can claim the pages it actually covers instead of inheriting
    #: the parent's whole span.
    page_marks: list[tuple[int, int]] = field(default_factory=list)
    chunk_type: ChunkType = ChunkType.TEXT
    atomic: bool = False  # table / image — never merged into prose, never split by us

    @property
    def start_page(self) -> int | None:
        return min(self.pages) if self.pages else None


@chunking_registry.register("structured_section")
class StructuredSectionChunker(BaseChunker):
    """Structure-aware chunker keyed on headings + leaf markers."""

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap_rate: float = 0.0,
        length_function: Callable[[str], int] | None = None,
        *,
        min_tokens: int | None = None,
        max_tokens: int | None = None,
        hard_max_tokens: int | None = None,
        inline_threshold: int | None = None,
        heading_keywords: tuple[str, ...] | list[str] | None = None,
        leaf_patterns: tuple[str, ...] | list[str] | None = None,
        prepend_heading_path: bool = True,
        layout: str = "auto",
        **kwargs: Any,
    ) -> None:
        # Overlap is forced to 0: leaves are atomic, so replaying a tail would
        # duplicate whole articles across chunks (the defect this replaces).
        super().__init__(
            chunk_size=chunk_size,
            chunk_overlap_rate=0.0,
            length_function=length_function,
            **kwargs,
        )
        # Sizing follows the current evidence sweet spot: ~512 target, with a
        # min/max band (≈128 / ≈768 at target 512) so tiny sibling sections pack
        # up and only genuinely oversize leaves get split.
        self.target_tokens = chunk_size
        self.max_tokens = max_tokens or int(chunk_size * 1.5)
        self.min_tokens = min_tokens if min_tokens is not None else max(1, chunk_size // 4)
        # A *safety* bound, distinct from ``max_tokens``. ``max_tokens`` is the
        # packing ceiling for prose; atomic units (a figure caption, a table)
        # are exempt from it, because a fragment of one is not a smaller version
        # of it — half a caption has lost the figure it describes, and half a
        # table has lost its column headers. ``hard_max_tokens`` exists only to
        # stop a pathological unit (a looping VLM caption) from silently
        # overflowing the embedder's context window, so it is derived from that
        # window by the caller, not from ``chunk_size``. ``None`` disables it:
        # atomic units are then never force-split.
        self.hard_max_tokens = hard_max_tokens
        # The safety bound OUTRANKS the packing band. Left beside it, the bound
        # was enforced in ``_pack`` and then undone by ``_merge_small`` — the
        # split pieces land under ``min_tokens``, share a page, and fold back
        # into a single unit whose only ceiling was the (larger) ``max_tokens``.
        # It also never reached prose at all, since ``_enforce_ceiling`` runs
        # only on atomic units. Collapsing the whole band under the bound closes
        # both holes at once: every downstream budget derives from
        # ``max_tokens``, so nothing can exceed the embedder's window again.
        # A no-op in the normal case (768 packing ceiling vs a 1023 bound on a
        # 2047-token window); it only bites on small-context embedders, which is
        # exactly where the bound exists to matter.
        if hard_max_tokens is not None and hard_max_tokens < self.max_tokens:
            self.max_tokens = hard_max_tokens
            self.target_tokens = min(self.target_tokens, self.max_tokens)
            self.min_tokens = min(self.min_tokens, max(1, self.max_tokens // 4))
        # Tables / image captions at or below this many tokens are inlined with
        # surrounding prose instead of becoming standalone chunks (matches the
        # recursive chunker's inline threshold).
        self._inline_threshold = inline_threshold if inline_threshold is not None else _INLINE_ELEMENT_TOKEN_THRESHOLD
        self.prepend_heading_path = prepend_heading_path
        self.layout = layout

        keywords = tuple(k.lower() for k in (heading_keywords or _DEFAULT_HEADING_KEYWORDS))
        self._keyword_level = {kw: _KEYWORD_LEVEL_BASE + i for i, kw in enumerate(keywords)}
        self._keyword_re = re.compile(
            r"^\s*(" + "|".join(re.escape(k) for k in keywords) + r")\b",
            re.IGNORECASE,
        )
        self._leaf_res = [re.compile(p, re.IGNORECASE) for p in (leaf_patterns or _DEFAULT_LEAF_PATTERNS)]

    # ------------------------------------------------------------------
    # ChunkingStrategy contract
    # ------------------------------------------------------------------
    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        content = self._content_from(document)
        if not content.strip():
            return []

        metadata = self._chunk_metadata_base(document, partition)
        filename = str(metadata.get("filename") or metadata.get("source") or "")

        paginated = self.layout == "paginated" or (self.layout == "auto" and self._looks_paginated(document))
        units = self._page_units(content) if paginated else self._build_units(content)
        candidates = self._single_chunk(units, filename)
        if candidates is None:
            candidates = self._pack(units, filename)
            # Not in paginated mode: a slide is routinely under min_tokens (the
            # corpus decks sit at 86-190 median), so the under-min merge would
            # fold the whole deck back into a handful of chunks and undo the
            # page boundary this mode exists to honour. _pack still splits an
            # oversize page.
            if not paginated:
                candidates = self._merge_small(candidates, filename)

        chunks: list[Chunk] = []
        for idx, unit in enumerate(candidates):
            # ``_build_units`` never emits a heading line as body — it updates
            # the stack and continues, so a heading can't be stranded as an
            # orphan chunk. That makes the header the *only* path by which
            # heading text reaches the index. With ``prepend_heading_path``
            # off, the breadcrumb is therefore kept inline at the top of the
            # body instead of dropped: BM25 is declared over ``text`` alone, so
            # a heading living only in ``metadata["hierarchy_path"]`` is
            # unreachable by dense *and* sparse retrieval. The flag drops the
            # verbose ``[Source | Page | Section]`` preamble, which is what an
            # operator turning it off is asking for — not the document's
            # structure.
            header = self._build_header(filename, unit.heading_path, unit.pages)
            body = unit.text.strip()
            if self.prepend_heading_path:
                text = f"{header}\n\n{body}" if header else body
            else:
                # No breadcrumb fallback needed any more — the heading lines are
                # in the body themselves, so turning the preamble off no longer
                # removes the document's structure from the index.
                text = body
                header = ""
            chunks.append(
                Chunk(
                    document_id=metadata.get("file_id", ""),
                    text=text,
                    chunk_index=idx,
                    chunk_type=unit.chunk_type,
                    partition=partition,
                    page_number=unit.start_page,
                    token_count=self.length_function(text),
                    header=header or None,
                    content=body,
                    metadata={
                        **{k: v for k, v in metadata.items() if k not in ("file_id", "partition")},
                        "hierarchy_path": list(unit.heading_path),
                        "section_title": unit.heading_path[-1] if unit.heading_path else "",
                        "page_range": _page_range(unit.pages),
                    },
                )
            )
        return chunks

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _looks_paginated(self, document: ProcessedDocument) -> bool:
        """Whether a *page* is this document's natural chunk boundary.

        Measured on the parsed pages, not on file type: what matters is
        whether a page is small and self-contained enough to be one chunk, not
        whether it was authored in PowerPoint. A dense slide deck reads like a
        report and should be chunked like one.

        The median, not the mean: one appendix page must not drag a 24-slide
        deck out of the bucket, and one title page must not drag a report into
        it. ``short_page_fraction`` separates the two where the median is
        close — a deck is uniformly sparse, a report has a few sparse pages
        among dense ones.
        """
        pages = [b for b in document.text_blocks if (b.text or "").strip()]
        if len(pages) < _MIN_PAGES_FOR_DETECTION:
            return False
        counts = [self.length_function(b.text) for b in pages]
        if statistics.median(counts) > _MAX_MEDIAN_TOKENS_PER_PAGE:
            return False
        short = sum(1 for c in counts if c < _SHORT_PAGE_TOKENS) / len(counts)
        return short >= _MIN_SHORT_PAGE_FRACTION

    def _page_units(self, content: str) -> list[_Unit]:
        """One unit per page, for a paginated document.

        The heading stack is still tracked across pages so each page keeps a
        breadcrumb, and headings stay in the body as everywhere else — only the
        *boundary* rule changes. Size is still enforced downstream: an oversize
        page splits and an under-min page merges with its neighbour, so
        "one chunk per page" is the default shape rather than a guarantee.
        """
        units: list[_Unit] = []
        stack: list[_Heading] = []
        page = 1
        buf: list[str] = []

        def flush(page_number: int) -> None:
            text = "\n".join(buf).strip()
            if text:
                units.append(
                    _Unit(
                        heading_path=list(stack_path(stack)),
                        text=text,
                        tokens=self.length_function(text),
                        pages={page_number},
                        page_marks=[(0, page_number)],
                        chunk_type=ChunkType.TEXT,
                    )
                )
            buf.clear()

        for raw_line in content.split("\n"):
            marker = PAGE_RE.fullmatch(raw_line.strip())
            if marker:
                flush(page)
                page = int(marker.group(1)) + 1
                continue
            level_text = self._heading(raw_line)
            if level_text is not None:
                _push_heading(stack, level_text[0], level_text[1])
            if raw_line.strip():
                buf.append(_HTML_TAG_RE.sub("", raw_line).strip() if level_text is not None else raw_line)
            elif buf and buf[-1] != "":
                buf.append("")
        flush(page)
        return units

    def _single_chunk(self, units: list[_Unit], filename: str) -> list[_Unit] | None:
        """The whole document as one chunk when it already fits.

        A short document has no boundary problem to solve: splitting it buys
        nothing and costs the reader the context of the neighbouring text. Only
        fires when everything fits under the ceiling *including* the header.
        """
        if not units:
            return None
        total = sum(u.tokens for u in units) + 2 * (len(units) - 1)
        path = units[0].heading_path
        for unit in units[1:]:
            path = _common_prefix(path, unit.heading_path)
        if total > self._effective_max(filename, path, {p for u in units for p in u.pages}):
            return None
        # Same guard as the merge routes: collapsing must not erase a section
        # breadcrumb. Four articles under four different Titres share no
        # ancestor, so folding them into one chunk would leave hierarchy_path
        # empty and put unrelated sections in one vector — small document or
        # not, that is the defect this strategy exists to remove.
        if not path and any(u.heading_path for u in units):
            return None
        merged = _copy_unit(units[0])
        merged.heading_path = list(path)
        for unit in units[1:]:
            _absorb(merged, unit, path=path)
        merged.tokens = total
        return [merged]

    # ------------------------------------------------------------------
    # Step 1 — parse the document into ordered structural units
    # ------------------------------------------------------------------
    def _build_units(self, content: str) -> list[_Unit]:
        """Walk the document in order, emitting one ``_Unit`` per leaf/atomic block.

        A heading line updates the heading stack but is emitted only through the
        chunk header — never as its own body — so a heading can never be
        stranded as an orphan chunk. Any prose that follows a heading before the
        next marker becomes that section's intro unit.
        """
        units: list[_Unit] = []
        stack: list[_Heading] = []
        page = 1

        # Current open leaf being accumulated (marker line + its body lines).
        buf_path: list[str] = []
        buf_lines: list[str] = []
        buf_pages: set[int] = set()
        buf_marks: list[tuple[int, int]] = []
        buf_len = 0
        buf_has_body = False

        def add_line(line: str, line_page: int | None = None, *, heading: bool = False) -> None:
            """Append a body line, remembering where each page starts in it."""
            nonlocal buf_len, buf_has_body
            if not heading and line.strip():
                buf_has_body = True
            if line_page is not None:
                buf_marks.append((buf_len, line_page))
                buf_pages.add(line_page)
            buf_lines.append(line)
            buf_len += len(line) + 1  # the "\n" the join will insert

        def flush(force: bool = False) -> None:
            """Emit the open unit. A buffer holding only headings is kept, not
            emitted: consecutive headings then pile into the unit their body
            eventually opens, instead of becoming an orphan heading chunk."""
            nonlocal buf_lines, buf_pages, buf_marks, buf_len, buf_has_body
            if not (buf_has_body or force):
                return
            text = "\n".join(buf_lines).strip()
            if text:
                for entry in stack:
                    entry.used = True
                units.append(
                    _Unit(
                        heading_path=list(buf_path),
                        text=text,
                        tokens=self.length_function(text),
                        pages=set(buf_pages) or {page},
                        page_marks=list(buf_marks),
                        chunk_type=ChunkType.TEXT,
                    )
                )
            buf_lines = []
            buf_pages = set()
            buf_marks = []
            buf_len = 0
            buf_has_body = False

        for element in split_md_elements(content):
            if element.type in ("table", "image"):
                # Non-informative images (the captioner's "[Image Placeholder]")
                # are dropped, matching the recursive chunker — otherwise every
                # blank logo becomes a chunk.
                if element.type == "image":
                    if is_placeholder_image(element.content):
                        continue
                    # Keep the caption, not the marker: the dense vector is
                    # computed over the chunk text and that text is what
                    # reaches the LLM, so a retained "[Image Placeholder]"
                    # would read as caption content. (#866 does the same on the
                    # recursive path.)
                    element.content = strip_placeholder_markers(element.content)
                # Small tables / image captions inline with the surrounding prose
                # (like recursive's _prepare_md_elements) so a slide's handful of
                # logos don't each become their own tiny chunk; only large ones
                # stay atomic (a big table splits via chunk_table downstream).
                if self.length_function(element.content) <= self._inline_threshold:
                    add_line(element.content.strip(), element.page_number or page)
                else:
                    flush(force=True)
                    for entry in stack:
                        entry.used = True
                    units.append(self._atomic_unit(element, list(stack_path(stack)), page))
                continue

            for raw_line in dewrap_paragraphs(element.content).split("\n"):
                page_match = PAGE_RE.fullmatch(raw_line.strip())
                if page_match:
                    # Content AFTER ``[PAGE_k]`` is page k+1 (see markdown_utils).
                    page = int(page_match.group(1)) + 1
                    continue

                level_text = self._heading(raw_line)
                if level_text is not None:
                    level, text = level_text
                    flush()
                    _push_heading(stack, level, text)
                    # The heading opens the body of the unit it introduces, the
                    # way recursive_splitter keeps heading lines inline. Holding
                    # it only in the breadcrumb made the breadcrumb its sole
                    # home, so every path-shortening step deleted it outright:
                    # _merge_small taking a _common_prefix dropped the differing
                    # leaf, a sibling heading popped it before any body flushed,
                    # and prepend_heading_path=False removed the last copy.
                    # Coverage against the source ran 91-99% where
                    # recursive_splitter ran ~100%.
                    #
                    # The body copy keeps the source line's markdown — the
                    # ``#`` depth is the cheapest signal of nesting there is,
                    # the chunk is markdown-rendered downstream, and
                    # recursive_splitter keeps heading lines verbatim, so a
                    # flattened copy would silently degrade every chunk of a
                    # partition that switched. Only the inline HTML the parsers
                    # leave behind (Marker's ``<span id="page-46-0">`` anchors)
                    # is removed. ``_clean_heading`` output stays the right
                    # thing for the breadcrumb and ``hierarchy_path``.
                    add_line(_HTML_TAG_RE.sub("", raw_line).strip(), page, heading=True)
                    buf_path = list(stack_path(stack))
                    continue

                if self._is_leaf(raw_line):
                    flush()
                    buf_path = list(stack_path(stack))

                if raw_line.strip():
                    # A markdown-heading line still here is one ``_heading``
                    # rejected as non-structural (caption, sentence, …); keep its
                    # text as body but drop the ``#``/emphasis markup so the body
                    # reads clean and isn't re-detected as a heading downstream.
                    add_line(_clean_heading(raw_line) if _MD_HEADING_RE.match(raw_line) else raw_line, page)
                elif buf_lines and buf_lines[-1] != "":
                    # Keep the paragraph break. Dropping blank lines left the
                    # body joined by single newlines, so ``_greedy_split``'s
                    # paragraph rung (``\n{2,}``) never matched and every
                    # oversize unit fell straight through to the sentence
                    # ladder, which rejoins with " " — welding headings into the
                    # middle of prose lines where they stop being detectable as
                    # headings at all.
                    add_line("")
            flush()

        flush(force=True)
        return units

    def _atomic_unit(self, element: MDElement, heading_path: list[str], page: int) -> _Unit:
        text = element.content.strip()
        ctype = ChunkType.TABLE if element.type == "table" else ChunkType.IMAGE_CAPTION
        return _Unit(
            heading_path=heading_path,
            text=text,
            tokens=self.length_function(text),
            pages={element.page_number or page},
            chunk_type=ctype,
            atomic=True,
        )

    def _heading(self, line: str) -> tuple[int, str] | None:
        # A leaf marker (e.g. an Article) is *content*, even when the parser
        # emitted it as a markdown heading ("#### **Article L110-5**"). It must
        # NOT become a container heading, or its identifier would live only in
        # the breadcrumb and be dropped when short sibling articles merge —
        # losing the single most important retrieval key of a legal article.
        if self._is_leaf(line):
            return None
        md = _MD_HEADING_RE.match(line)
        if md:
            text = _clean_heading(md.group(2))
            # A structural keyword (Partie/Livre/Titre/Chapitre/…) carries the
            # real nesting depth in its rank, so prefer it over the parser's ``#``
            # depth. Marker emits an entire legal hierarchy at a single ``#``
            # (``# Livre I``, ``# Titre I``, ``# Chapitre …`` all level 1); taking
            # the flat markdown level makes those same-level headings pop one
            # another off the stack, collapsing the whole hierarchy to its
            # innermost heading and dropping every ancestor — including the
            # document title — from the breadcrumb.
            kw_level = self._keyword_level_for(text)
            if not _looks_like_heading(text, keyword_led=kw_level is not None):
                return None
            level = kw_level if kw_level is not None else _MD_LEVEL_BASE + len(md.group(1))
            return level, text
        kw = self._keyword_re.match(line)
        if kw:
            text = _clean_heading(line)
            if not _looks_like_heading(text, keyword_led=True):
                return None
            return self._keyword_level[kw.group(1).lower()], text
        return None

    def _keyword_level_for(self, text: str) -> int | None:
        """Nesting level if ``text`` opens with a structural keyword, else None."""
        kw = self._keyword_re.match(text)
        return self._keyword_level[kw.group(1).lower()] if kw else None

    def _is_leaf(self, line: str) -> bool:
        # Strip a markdown heading prefix + emphasis so "#### **Article L110-5**"
        # matches the same leaf patterns as a plain "Article L110-5" line.
        core = _EMPHASIS_RE.sub("", _HEADING_PREFIX_RE.sub("", line)).strip()
        return any(r.match(core) for r in self._leaf_res)

    # ------------------------------------------------------------------
    # Step 2 — pack consecutive units up to the token target
    # ------------------------------------------------------------------
    def _pack(self, units: list[_Unit], filename: str) -> list[_Unit]:
        out: list[_Unit] = []
        cur: _Unit | None = None

        for unit in units:
            if unit.atomic:
                cur = self._flush_into(out, cur)
                out.extend(self._emit_atomic(unit, filename))
                continue
            if unit.tokens > self._effective_max(filename, unit.heading_path, unit.pages):
                cur = self._flush_into(out, cur)
                out.extend(self._split_oversize(unit, filename))
                continue
            if cur is None:
                cur = _copy_unit(unit)
            elif cur.heading_path == unit.heading_path and cur.tokens + unit.tokens <= self._packing_budget(
                filename, cur.heading_path, cur.pages | unit.pages
            ):
                _absorb(cur, unit)
            else:
                out.append(cur)
                cur = _copy_unit(unit)
        if cur is not None:
            out.append(cur)
        return out

    def _header_reserve(self, filename: str, heading_path: list[str], pages: set[int]) -> int:
        """Exact token cost of the prepended ``[Source | Page | Section]`` header
        (plus its blank-line separator), reserved from the body budget so the
        embedded ``chunk.text`` (header + body) stays within target/max.

        Measured from the real header rather than estimated: a flat allowance
        for the ``Source:``/``Page:`` fields silently under-reserves whenever
        the filename is long, and the body then overshoots ``max`` by exactly
        the amount the estimate missed.
        """
        if not self.prepend_heading_path:
            return 0
        header = self._build_header(filename, heading_path, pages)
        return self.length_function(f"{header}\n\n") if header else 0

    def _packing_budget(self, filename: str, heading_path: list[str], pages: set[int]) -> int:
        """Body budget so a packed chunk lands near ``target`` *including* header."""
        # Floored at 1, not ``min_tokens``: these budgets are *body* budgets and
        # the header is added on top, so a floor at ``min_tokens`` would hand
        # back a budget that already exceeds the ceiling once the band has
        # collapsed under ``hard_max_tokens``. In the normal band the floor
        # never binds anyway (768 - a ~50-token header still clears 128).
        return max(1, self.target_tokens - self._header_reserve(filename, heading_path, pages))

    def _effective_max(self, filename: str, heading_path: list[str], pages: set[int]) -> int:
        """Body ceiling so ``chunk.text`` (body + header) never exceeds ``max``."""
        return max(1, self.max_tokens - self._header_reserve(filename, heading_path, pages))

    def _emit_atomic(self, unit: _Unit, filename: str) -> list[_Unit]:
        """A table over the (header-adjusted) max is split by ``chunk_table``;
        whatever still exceeds the ceiling then goes through
        ``_enforce_ceiling``."""
        emax = self._effective_max(filename, unit.heading_path, unit.pages)
        if unit.chunk_type is ChunkType.TABLE and unit.tokens > emax:
            element = MDElement(type="table", content=unit.text, page_number=unit.start_page)
            subs = chunk_table(element, chunk_size=emax, length_function=self.length_function)
            pieces = [
                _Unit(
                    heading_path=list(unit.heading_path),
                    text=s.content.strip(),
                    tokens=self.length_function(s.content),
                    pages={s.page_number} if s.page_number else set(unit.pages),
                    chunk_type=ChunkType.TABLE,
                    atomic=True,
                )
                for s in subs
            ]
        else:
            pieces = [unit]
        return self._enforce_ceiling(pieces, filename)

    def _enforce_ceiling(self, units: list[_Unit], filename: str) -> list[_Unit]:
        """Safety net against a unit large enough to overflow the embedder.

        This deliberately does **not** enforce ``max_tokens``. That is the
        packing ceiling for prose; an atomic unit is exempt from it, because a
        fragment of an atomic unit is not a smaller version of it. Piece 2 of
        *"The chart illustrates the relationship between…"* has lost its
        subject, its figure, and any hint of what it describes — a chunk that
        embeds well and reads as a non-sequitur. Measured on the marker corpus,
        enforcing ``max_tokens`` here split 55 captions into 78 fragments, and
        bought nothing: the largest unsplit caption was 1,211 tokens, so no
        caption came close to any real embedding limit.

        ``hard_max_tokens`` is the bound that does matter — content past the
        embedder's window is silently truncated, which is real data loss. It is
        derived from that window by the caller (see ``create_chunker``), so a
        partition pointing at a small-context embedder gets a tighter bound.
        ``None`` disables the net entirely.

        Tables are never force-split even above the bound: ``chunk_table``
        already divides them losslessly on row boundaries, replaying the column
        header. What reaches here is a single row too large to divide, and
        cutting it mid-sentence yields a fragment that is neither a valid row
        nor carries its headers. A physical-row fallback was measured and made
        things worse — over-max chunks 113 → 80 but total excess tokens doubled
        (16.6k → 33.3k), duplication 1.75% → 2.79%, orphan headings 5 → 22.
        """
        if self.hard_max_tokens is None:
            return units
        out: list[_Unit] = []
        for unit in units:
            if unit.tokens <= self.hard_max_tokens or unit.chunk_type is ChunkType.TABLE:
                out.append(unit)
                continue
            # Cut as few times as possible: pack to the hard bound itself rather
            # than back down to the prose target, so a caption that trips the
            # net is halved, not shredded.
            out.extend(
                self._split_oversize(
                    unit,
                    filename,
                    chunk_type=unit.chunk_type,
                    target=self.hard_max_tokens,
                    ceiling=self.hard_max_tokens,
                )
            )
        return out

    def _split_oversize(
        self,
        unit: _Unit,
        filename: str,
        *,
        chunk_type: ChunkType = ChunkType.TEXT,
        target: int | None = None,
        ceiling: int | None = None,
    ) -> list[_Unit]:
        """Split a single over-``max`` leaf at paragraph, then sentence boundaries,
        to header-adjusted target/max so each piece + header stays within max.

        ``chunk_type`` (and the unit's ``atomic`` flag) carry through so an
        oversize image caption stays typed as one — and stays unmergeable with
        neighbouring prose — instead of being relabelled plain text.

        ``target``/``ceiling`` override the prose budgets; the hard-bound safety
        net passes the bound itself so a tripped unit is cut as few times as
        possible instead of being packed back down to the prose target.
        """
        path = unit.heading_path
        reserve = self._header_reserve(filename, path, unit.pages)
        pieces = _greedy_split(
            unit.text,
            # Floored at ``min_tokens`` like ``_packing_budget`` /
            # ``_effective_max``: a floor of 1 let a long filename or deep
            # breadcrumb eat the whole budget and emit one-token pieces.
            max(1, target - reserve) if target is not None else self._packing_budget(filename, path, unit.pages),
            max(1, ceiling - reserve) if ceiling is not None else self._effective_max(filename, path, unit.pages),
            self.length_function,
        )
        page_sets = _attribute_pages(unit, pieces)
        return [
            _Unit(
                heading_path=list(unit.heading_path),
                text=piece.strip(),
                tokens=self.length_function(piece),
                pages=pages,
                chunk_type=chunk_type,
                atomic=unit.atomic,
            )
            for piece, pages in zip(pieces, page_sets, strict=True)
        ]

    @staticmethod
    def _flush_into(out: list[_Unit], cur: _Unit | None) -> None:
        if cur is not None:
            out.append(cur)
        return None

    # ------------------------------------------------------------------
    # Step 3 — merge under-min neighbours (combined <= max)
    # ------------------------------------------------------------------
    def _merge_small(self, units: list[_Unit], filename: str) -> list[_Unit]:
        """Fold an under-``min`` neighbour into the previous chunk when they are
        structurally compatible (same section, or one path a prefix of the
        other) and the result stays under ``max``.

        Merging *backward* (into the already-emitted previous chunk) keeps a
        stray small leaf with its own section rather than pulling it forward
        across a heading boundary into the next one — which would strand it
        under a wrong/shallower heading path.

        The ceiling is the *header-adjusted* max: ``_Unit.tokens`` counts the
        body only, while the emitted ``chunk.text`` also carries the
        ``[Source | Page | Section]`` header. Comparing bodies against the raw
        ``max_tokens`` here let a merge fill the body to the ceiling and then
        push past it once the header was prepended — worst on deep heading
        paths, where the header is largest.
        """
        out: list[_Unit] = []
        for unit in units:
            if out:
                prev = out[-1]
                merged_path = _common_prefix(prev.heading_path, unit.heading_path)
                if self._may_merge(prev, unit, filename, merged_path):
                    # Independent of which route allowed the merge: never let the
                    # result end up with no breadcrumb when either input had one.
                    # Keeping the surviving path is strictly better than dropping
                    # the section from both the header and ``hierarchy_path``,
                    # where it becomes unreachable by dense and sparse search
                    # alike (BM25 is declared over ``text`` only).
                    _absorb(prev, unit, path=merged_path or prev.heading_path or unit.heading_path)
                    continue
            out.append(_copy_unit(unit))
        return out

    def _may_merge(self, prev: _Unit, unit: _Unit, filename: str, merged_path: list[str]) -> bool:
        """Whether two adjacent units may fold together.

        Two gates always apply: at least one side is under ``min_tokens``, and
        the result stays under the header-adjusted ceiling.

        Beyond that there are two routes. **Same page** is the permissive one —
        a slide is page-atomic, so its title, one-line label, figure caption and
        table belong in one chunk, and requiring structural compatibility there
        left every slide as three chunks (two of them noise). Restricting this
        route to a shared page is what keeps it safe: it cannot chain content
        across slides or across distant sections.

        Otherwise the structural route applies, which is stricter: tables stay
        out (a table glued to unrelated prose loses the column headers that make
        it readable) and the heading paths must be compatible.
        """
        if prev.tokens >= self.min_tokens and unit.tokens >= self.min_tokens:
            return False
        if prev.tokens + unit.tokens > self._effective_max(filename, merged_path, prev.pages | unit.pages):
            return False
        if prev.pages & unit.pages and _keeps_context(prev.heading_path, unit.heading_path):
            return True
        return self._may_absorb(prev) and self._may_absorb(unit) and _compatible(prev.heading_path, unit.heading_path)

    def _may_absorb(self, unit: _Unit) -> bool:
        """Whether a unit can take part in small-neighbour merging.

        Atomic units are normally excluded, but image captions must be allowed
        in at any size. A slide is heading + one-line label + figure + label,
        and the labels are 9-70 tokens while the figure's caption is 200-500,
        so the labels sit *between large atomic captions* and could never reach
        a mergeable neighbour (``_merge_small`` only merges into the immediately
        preceding unit). Every slide stayed three chunks, two of them noise:
        29% of a Renault deck's chunks fell under the floor against 0% for
        ``recursive_splitter`` — the one document class where the old chunker
        won. On a slide the label belongs with the figure it labels, so letting
        the caption absorb it is also the semantically right answer.

        Tables are excluded here, so a table fragment is never glued to
        unrelated prose and left without the column headers that make it
        readable. Note this gate governs only the *structural* route — the
        same-page route in ``_may_merge`` returns before consulting it, so a
        table may still absorb a label sharing its page. That is deliberate: on
        a slide the label belongs to the table.
        """
        return not unit.atomic or unit.chunk_type is ChunkType.IMAGE_CAPTION

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    @staticmethod
    def _build_header(filename: str, heading_path: list[str], pages: set[int]) -> str:
        parts: list[str] = []
        if filename:
            parts.append(f"Source: {filename}")
        page_range = _page_range(pages)
        if page_range:
            parts.append(f"Page: {page_range}")
        if heading_path:
            parts.append(f"Section: {' > '.join(heading_path)}")
        return f"[{' | '.join(parts)}]" if parts else ""


# ---------------------------------------------------------------------------
# Free helpers (kept out of the class — pure, testable)
# ---------------------------------------------------------------------------
def stack_path(stack: list[_Heading]) -> list[str]:
    return [entry.text for entry in stack]


def _push_heading(stack: list[_Heading], level: int, text: str) -> list[str]:
    """Push a heading, returning any popped heading that never reached a chunk.

    A heading only reaches the index through the breadcrumb of a unit flushed
    beneath it. A heading immediately followed by a sibling — a run of
    ``Chapitre`` titles in a legal code, or the stacked ``####`` lines that make
    up a slide — is popped before any body is flushed, so its text appeared in
    no chunk at all: not in ``text``, not in the header, not in
    ``hierarchy_path``. That is silent content loss, measured at 7% of a slide
    deck and ~1,200 words of a legal code. The caller keeps the returned text by
    putting it in the next unit's body, in document order.
    """
    dropped: list[str] = []
    while stack and stack[-1].level >= level:
        popped = stack.pop()
        if not popped.used:
            dropped.append(popped.text)
    stack.append(_Heading(level=level, text=text))
    return dropped


def _attribute_pages(unit: _Unit, pieces: list[str]) -> list[set[int]]:
    """Pages each piece of a split unit actually covers.

    Copying the parent's whole page set into every piece made
    ``Chunk.page_number`` (``min(pages)``) the unit's *first* page for all of
    them, and ``page_range`` its full span — so on a four-page unit every chunk
    reported ``page=1, page_range='1-4'``, including pieces whose text is on
    page 4. That is what the citation UI shows and what ``/extract`` anchors on,
    so the error is user-visible and grows with document length.

    ``_Unit.page_marks`` records where each page starts inside ``unit.text``, so
    a piece can be located and given the pages spanning it — plus the page in
    effect where it begins, which a piece starting mid-page would otherwise
    miss. Pieces are located by their opening line rather than by arithmetic:
    ``_greedy_split`` normalises whitespace, so offsets do not survive it.
    """
    if not unit.page_marks:
        return [set(unit.pages) for _ in pieces]
    text = unit.text
    out: list[set[int]] = []
    cursor = 0
    for piece in pieces:
        probe = next((line for line in piece.splitlines() if line.strip()), "")[:60]
        start = text.find(probe, cursor) if probe else -1
        if start < 0:
            start = cursor
        end = start + len(piece)
        pages = {page for offset, page in unit.page_marks if start <= offset < end}
        opening = [page for offset, page in unit.page_marks if offset <= start]
        if opening:
            pages.add(opening[-1])
        out.append(pages or set(unit.pages))
        cursor = max(cursor, start)
    return out


def _copy_unit(unit: _Unit) -> _Unit:
    return _Unit(
        heading_path=list(unit.heading_path),
        text=unit.text,
        tokens=unit.tokens,
        pages=set(unit.pages),
        chunk_type=unit.chunk_type,
        atomic=unit.atomic,
    )


def _absorb(cur: _Unit, unit: _Unit, *, path: list[str] | None = None) -> None:
    cur.text = f"{cur.text}\n\n{unit.text}".strip()
    cur.tokens = cur.tokens + unit.tokens
    cur.pages |= unit.pages
    if unit.chunk_type is not ChunkType.TEXT and cur.chunk_type is ChunkType.TEXT:
        cur.chunk_type = unit.chunk_type
    # Atomicity travels with the type. Without this a text unit that swallowed
    # a table came out typed TABLE but atomic=False, so ``_may_absorb`` let it
    # go on absorbing unrelated prose from other pages — the table protection
    # applies to a unit that *is* a table, not to one that merely started as
    # prose next to one.
    cur.atomic = cur.atomic or unit.atomic
    if path is not None:
        cur.heading_path = list(path)


def _keeps_context(a: list[str], b: list[str]) -> bool:
    """Whether merging two units would preserve a section breadcrumb.

    The same-page route is permissive because a slide *is* a section. That
    reasoning does not carry to dense text, where several short units per page
    under different headings is the norm — a legal code being the extreme case.
    There, merging on page alone produced one chunk holding a tax article, a
    public-health article, a labour article and a criminal article, under a
    ``_common_prefix`` of ``[]``: every ``Titre``/``Chapitre`` erased from the
    embedded header *and* from ``hierarchy_path``, which is the orphan-heading
    defect this strategy exists to remove, reintroduced one pass later.

    So: two units that both carry a path may only merge when they still share
    an ancestor. A path-less unit (a slide label, a cover block) has no
    breadcrumb to lose and is still free to join its neighbour.
    """
    if not a or not b:
        return True
    return bool(_common_prefix(a, b))


def _common_prefix(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return out


def _compatible(a: list[str], b: list[str]) -> bool:
    """True when two heading paths may share a chunk without crossing a section
    boundary: identical, one a prefix of the other, or siblings (same parent,
    differing only in the leaf). Prevents merging e.g. ``Titre I > …`` with
    ``Titre II > …``.
    """
    if a == b:
        return True
    # A heading-less unit (document preamble, cover page, stray block before the
    # first heading) is NOT compatible with everything. The prefix test below
    # would say it is — ``long[:0] == []`` always holds — turning any path-less
    # unit into a magnet that chains unrelated sections into one chunk, under a
    # ``_common_prefix`` of ``[]`` that discards the real section entirely.
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if long[: len(short)] == short:
        return True
    return len(a) == len(b) and a[:-1] == b[:-1]


def _page_range(pages: set[int]) -> str:
    valid = sorted(p for p in pages if p is not None)
    if not valid:
        return ""
    return str(valid[0]) if valid[0] == valid[-1] else f"{valid[0]}-{valid[-1]}"


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Fallback ladder for a paragraph with no sentence terminators — long legal
# recitals and enumerations run for hundreds of tokens on semicolons or commas
# alone. Tried in order of decreasing semantic strength before word-wrapping,
# which is the only splitter that can land mid-sentence.
_CLAUSE_RES = (
    re.compile(r"(?<=;)\s+"),
    re.compile(r"(?<=:)\s+"),
    re.compile(r"\s+(?=[—–]\s)"),
    re.compile(r"(?<=,)\s+"),
)
_SPLIT_LADDER = (_SENTENCE_RE, *_CLAUSE_RES)


def _greedy_split(text: str, target: int, max_tokens: int, length_function: Callable[[str], int]) -> list[str]:
    """Pack paragraphs (then sentences for an over-``max`` paragraph) to ``target``."""

    def descend(item: str, levels: tuple[re.Pattern[str], ...]) -> list[str]:
        """Split one over-``max`` item on the strongest separator it actually
        contains, then keep the weaker ones in reserve for its own oversize
        pieces. Word-wrapping is the last resort because it is the only step
        that can cut mid-sentence."""
        for i, splitter in enumerate(levels):
            parts = splitter.split(item)
            if len(parts) > 1:
                return pack(parts, " ", levels[i + 1 :])
        return _hard_wrap(item, target, length_function)

    def pack(items: list[str], joiner: str, levels: tuple[re.Pattern[str], ...]) -> list[str]:
        out: list[str] = []
        buf: list[str] = []
        size = 0
        for item in items:
            it = length_function(item)
            if buf and size + it > target:
                out.append(joiner.join(buf))
                buf, size = [], 0
            if it > max_tokens and not buf:
                out.extend(descend(item, levels))
                continue
            buf.append(item)
            size += it
        if buf:
            out.append(joiner.join(buf))
        return out

    sanitized = sanitize_text(text, max_consecutive_newlines=2)
    return _rehome_trailing_headings(pack(re.split(r"\n{2,}", sanitized), "\n\n", _SPLIT_LADDER))


def _rehome_trailing_headings(pieces: list[str]) -> list[str]:
    """Move a heading stranded at a piece's end onto the following piece.

    Splitting long *prose* rarely lands on a heading, but VLM image captions
    carry their own ``###`` sub-headings, so packing paragraphs could end a
    piece on one — stranding the heading away from the content it introduces
    (worst case: a piece that is an ``<image_description>`` opener plus a bare
    heading). Rule 4 of the strategy is that a heading always travels with its
    body, so hand it forward rather than leaving it orphaned.
    """
    out = list(pieces)
    # Walk backwards so a run of consecutive headings cascades forward in one
    # pass, and so a piece that is *only* a heading (the worst case — packing
    # can emit a bare ``### Key Trends`` as its own 3-token piece) empties out
    # and is dropped below rather than being left as a heading-only chunk.
    for i in range(len(out) - 2, -1, -1):
        lines = out[i].rstrip().splitlines()
        cut = len(lines)
        while cut > 0 and _CAPTION_HEADING_RE.match(lines[cut - 1]):
            cut -= 1
        if cut == len(lines):
            continue
        moved = "\n\n".join(line.strip() for line in lines[cut:])
        out[i] = "\n".join(lines[:cut]).rstrip()
        out[i + 1] = f"{moved}\n\n{out[i + 1].lstrip()}"
    return [piece for piece in out if piece.strip()]


def _hard_wrap(text: str, target: int, length_function: Callable[[str], int]) -> list[str]:
    """Absolute last resort for a break-less blob (no paragraph/sentence breaks):
    split the words into groups that each measure at or below ``target``.

    The word count is only an *estimate* of the token count — a slice of
    token-dense words (URLs, long identifiers, numeric tables, CJK) tokenises
    far above the average, so slicing purely on ``ceil(tokens / target)`` could
    emit a piece over the ceiling and quietly break the guarantee this function
    exists to provide. So: slice on the estimate (cheap, one measurement), then
    verify every piece and re-pack only the ones that actually overflow.
    """
    words = text.split()
    if not words:
        return [text]
    groups = max(1, -(-length_function(text) // target))
    size = max(1, -(-len(words) // groups))
    out: list[str] = []
    for start in range(0, len(words), size):
        piece = " ".join(words[start : start + size])
        if length_function(piece) <= target:
            out.append(piece)
        else:
            out.extend(_pack_words(words[start : start + size], target, length_function))
    return out


def _pack_words(words: list[str], target: int, length_function: Callable[[str], int]) -> list[str]:
    """Pack words one at a time, measuring as we go, so no piece exceeds
    ``target``. A single word larger than ``target`` is emitted alone — there is
    nothing below a word to split on without corrupting it."""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for word in words:
        cost = length_function(word if not buf else f" {word}")
        if buf and size + cost > target:
            out.append(" ".join(buf))
            buf, size = [], 0
            cost = length_function(word)
        buf.append(word)
        size += cost
    if buf:
        out.append(" ".join(buf))
    return out
