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
markdown-``#``-only detector would miss every boundary. Patterns are
config-driven (``heading_patterns`` / ``leaf_patterns``) so the strategy
generalizes beyond French legal codes; markdown ``#`` headings are recognized
too.

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
    _IMAGE_PLACEHOLDER_MARKER,
    _INLINE_ELEMENT_TOKEN_THRESHOLD,
    BaseChunker,
    dewrap_paragraphs,
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
_MD_LEVEL_BASE = 0
_KEYWORD_LEVEL_BASE = 100


@dataclass
class _Unit:
    """A structural unit: a leaf (article/heading-intro) or an atomic block."""

    heading_path: list[str]
    text: str
    tokens: int
    pages: set[int] = field(default_factory=set)
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
        inline_threshold: int | None = None,
        heading_keywords: tuple[str, ...] | list[str] | None = None,
        leaf_patterns: tuple[str, ...] | list[str] | None = None,
        prepend_heading_path: bool = True,
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
        # Tables / image captions at or below this many tokens are inlined with
        # surrounding prose instead of becoming standalone chunks (matches the
        # recursive chunker's inline threshold).
        self._inline_threshold = inline_threshold if inline_threshold is not None else _INLINE_ELEMENT_TOKEN_THRESHOLD
        self.prepend_heading_path = prepend_heading_path

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

        units = self._build_units(content)
        candidates = self._pack(units, filename)
        candidates = self._merge_small(candidates, filename)

        chunks: list[Chunk] = []
        for idx, unit in enumerate(candidates):
            header = self._build_header(filename, unit.heading_path, unit.pages)
            body = unit.text.strip()
            text = f"{header}\n\n{body}" if (header and self.prepend_heading_path) else body
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
        stack: list[tuple[int, str]] = []  # (level, text)
        page = 1

        # Current open leaf being accumulated (marker line + its body lines).
        buf_path: list[str] = []
        buf_lines: list[str] = []
        buf_pages: set[int] = set()

        def flush() -> None:
            nonlocal buf_lines, buf_pages
            text = "\n".join(buf_lines).strip()
            if text:
                units.append(
                    _Unit(
                        heading_path=list(buf_path),
                        text=text,
                        tokens=self.length_function(text),
                        pages=set(buf_pages) or {page},
                        chunk_type=ChunkType.TEXT,
                    )
                )
            buf_lines = []
            buf_pages = set()

        for element in split_md_elements(content):
            if element.type in ("table", "image"):
                # Non-informative images (the captioner's "[Image Placeholder]")
                # are dropped, matching the recursive chunker — otherwise every
                # blank logo becomes a chunk.
                if element.type == "image" and _IMAGE_PLACEHOLDER_MARKER in element.content.lower():
                    continue
                # Small tables / image captions inline with the surrounding prose
                # (like recursive's _prepare_md_elements) so a slide's handful of
                # logos don't each become their own tiny chunk; only large ones
                # stay atomic (a big table splits via chunk_table downstream).
                if self.length_function(element.content) <= self._inline_threshold:
                    buf_lines.append(element.content.strip())
                    buf_pages.add(element.page_number or page)
                else:
                    flush()
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
                    buf_lines.append(_clean_heading(raw_line) if _MD_HEADING_RE.match(raw_line) else raw_line)
                    buf_pages.add(page)
            flush()

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
        return max(self.min_tokens, self.target_tokens - self._header_reserve(filename, heading_path, pages))

    def _effective_max(self, filename: str, heading_path: list[str], pages: set[int]) -> int:
        """Body ceiling so ``chunk.text`` (body + header) never exceeds ``max``."""
        return max(self.min_tokens, self.max_tokens - self._header_reserve(filename, heading_path, pages))

    def _emit_atomic(self, unit: _Unit, filename: str) -> list[_Unit]:
        """A table over the (header-adjusted) max is split by ``chunk_table``."""
        emax = self._effective_max(filename, unit.heading_path, unit.pages)
        if unit.chunk_type is ChunkType.TABLE and unit.tokens > emax:
            element = MDElement(type="table", content=unit.text, page_number=unit.start_page)
            subs = chunk_table(element, chunk_size=emax, length_function=self.length_function)
            return [
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
        return [unit]

    def _split_oversize(self, unit: _Unit, filename: str) -> list[_Unit]:
        """Split a single over-``max`` leaf at paragraph, then sentence boundaries,
        to header-adjusted target/max so each piece + header stays within max."""
        path = unit.heading_path
        pieces = _greedy_split(
            unit.text,
            self._packing_budget(filename, path, unit.pages),
            self._effective_max(filename, path, unit.pages),
            self.length_function,
        )
        return [
            _Unit(
                heading_path=list(unit.heading_path),
                text=piece.strip(),
                tokens=self.length_function(piece),
                pages=set(unit.pages),
                chunk_type=ChunkType.TEXT,
            )
            for piece in pieces
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
                if (
                    not prev.atomic
                    and not unit.atomic
                    and (prev.tokens < self.min_tokens or unit.tokens < self.min_tokens)
                    and prev.tokens + unit.tokens <= self._effective_max(filename, merged_path, prev.pages | unit.pages)
                    and _compatible(prev.heading_path, unit.heading_path)
                ):
                    _absorb(prev, unit, path=merged_path)
                    continue
            out.append(_copy_unit(unit))
        return out

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
def stack_path(stack: list[tuple[int, str]]) -> list[str]:
    return [text for _, text in stack]


def _push_heading(stack: list[tuple[int, str]], level: int, text: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, text))


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
    if path is not None:
        cur.heading_path = list(path)


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


def _greedy_split(text: str, target: int, max_tokens: int, length_function: Callable[[str], int]) -> list[str]:
    """Pack paragraphs (then sentences for an over-``max`` paragraph) to ``target``."""

    def pack(items: list[str], joiner: str) -> list[str]:
        out: list[str] = []
        buf: list[str] = []
        size = 0
        for item in items:
            it = length_function(item)
            if buf and size + it > target:
                out.append(joiner.join(buf))
                buf, size = [], 0
            if it > max_tokens and not buf:
                out.extend(
                    pack(_SENTENCE_RE.split(item), " ") if joiner != " " else _hard_wrap(item, target, length_function)
                )
                continue
            buf.append(item)
            size += it
        if buf:
            out.append(joiner.join(buf))
        return out

    sanitized = sanitize_text(text, max_consecutive_newlines=2)
    return pack(re.split(r"\n{2,}", sanitized), "\n\n")


def _hard_wrap(text: str, target: int, length_function: Callable[[str], int]) -> list[str]:
    """Absolute last resort for a break-less blob (no paragraph/sentence breaks):
    split the words into ``ceil(tokens / target)`` roughly equal groups so every
    piece lands near ``target`` — never leaving a piece over ``max``."""
    words = text.split()
    if not words:
        return [text]
    groups = max(1, -(-length_function(text) // target))
    size = max(1, -(-len(words) // groups))
    return [" ".join(words[i : i + size]) for i in range(0, len(words), size)]
