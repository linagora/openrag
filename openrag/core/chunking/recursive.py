"""Recursive markdown-aware chunking strategy.

Pure domain logic — no LLM client, no Ray, no LangChain ``Document``.
The token-counting function is injected (``length_function``); the actual
text splitter is ``langchain.text_splitter.RecursiveCharacterTextSplitter``,
a pure utility kept until a stdlib-only replacement is in place.

Contextualization (the LLM-driven [CONTEXT] block prepended to each chunk)
lives in ``core/indexing/contextualize.py`` (Phase 5D) and is applied as a
separate stage by the orchestrator — not from inside the chunker.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.chunking.chunking_strategy import ChunkingStrategy
from core.chunking.markdown_utils import (
    PAGE_RE,
    MDElement,
    chunk_table,
    get_chunk_page_number,
    split_md_elements,
)
from core.chunking.registry import chunking_registry
from core.models.chunk import Chunk, ChunkType
from core.models.document import ProcessedDocument
from core.utils.text import sanitize_text

# Substring (case-insensitive) marking a "no useful content" image caption.
# Detection logic mirrors the legacy chunker, which skips these elements so
# they don't pollute the index.
_IMAGE_PLACEHOLDER_MARKER = "[image placeholder]"

# Strips the block's own wrapper so only the caption text is weighed.
_IMAGE_BLOCK_TAG_RE = re.compile(r"</?image_description>", re.IGNORECASE)
_IMAGE_PLACEHOLDER_RE = re.compile(re.escape(_IMAGE_PLACEHOLDER_MARKER), re.IGNORECASE)
# Removing a marker leaves the blank lines that surrounded it.
_COLLAPSE_BLANK_RE = re.compile(r"\n{3,}")
# Page markers closing a run of prose, with nothing after them to label.
_TRAILING_PAGE_MARKERS_RE = re.compile(r"(?:\[PAGE_\d+\]\s*)+$")


def is_placeholder_image(content: str) -> bool:
    """True when an image block carries no caption beyond the placeholder.

    ``wrap_caption`` emits one wrapper per image, so the shape that matters is
    the marker and real caption text inside the *same* wrapper: a composite
    figure where the VLM could describe some parts and not others. Dropping the
    whole block on any occurrence of the marker deleted that caption text along
    with it — on the corpus, a 132-token chart caption
    ("92 000 COLLABORATEURS…") and 225 words of a tourism description,
    silently, from the index, and from *both* chunkers since they share this
    test. (Two *adjacent* wrappers were never affected: ``IMAGE_RE`` is
    non-greedy, so they parse as separate elements and only the placeholder-only
    one was dropped.)

    So a block is only skipped when nothing but the wrapper and the marker(s)
    remains. Anything else is caption text and is kept — see
    ``strip_placeholder_markers`` for removing the marker from it. The test is
    whitespace, not alphanumerics: a caption reduced to "©" or an em dash is
    still content, and a rule about not dropping content should not carve out
    an exception for symbols.
    """
    if _IMAGE_PLACEHOLDER_MARKER not in content.lower():
        return False
    remainder = _IMAGE_PLACEHOLDER_RE.sub(" ", _IMAGE_BLOCK_TAG_RE.sub(" ", content))
    return not remainder.strip()


def strip_placeholder_markers(content: str) -> str:
    """Drop the marker(s) from a block that also carries real caption text.

    Keeping the caption must not smuggle the marker into the index with it: the
    dense vector is computed over the chunk text, and that text is what
    ``format_context`` puts in front of the LLM, so the marker would read as
    caption content. (The BM25 stop-word list in ``milvus_store`` already
    ignores it, so only the dense side and the prompt are affected.)

    Stripping before the size test also makes the inline-vs-standalone decision
    measure the caption rather than the marker's tokens.
    """
    return _COLLAPSE_BLANK_RE.sub("\n\n", _IMAGE_PLACEHOLDER_RE.sub("", content)).strip()


# Longest filename tokenized for a chunk's header. An upload is saved under its
# name, so the filesystem already stops it at 255 bytes; a name taken from a URL
# (MCP index_url) has no such limit.
_MAX_HEADER_FILENAME_CHARS = 255


def source_header(filename: str, chunk_size: int, length_function: Callable[[str], int]) -> str | None:
    """``[Source: <filename>]``, using at most half of *chunk_size*.

    The body keeps the other half: each chunk costs an embedding and maybe an
    LLM call, so a name must not be able to cut a document into thousands of
    pieces, nor push a chunk past *chunk_size*. A longer name is shortened,
    and a *chunk_size* with no room for one gets no header.
    """
    name = filename[:_MAX_HEADER_FILENAME_CHARS]
    limit = chunk_size // 2
    while name:
        header = f"[Source: {name}]"
        tokens = length_function(f"{header}\n\n")
        if tokens <= limit:
            return header
        name = name[: min(len(name) - 1, len(name) * limit // tokens)]
    return None


# Tables/images smaller than this token count are inlined with surrounding
# text rather than emitted as standalone chunks.
_INLINE_ELEMENT_TOKEN_THRESHOLD = 100

# A line that begins a markdown block (heading, list item, blockquote, table
# row, code fence, horizontal rule, or a synthetic [PAGE_N] marker). Such lines
# must keep their own line — they are NOT prose to be joined into a paragraph.
_BLOCK_LINE_RE = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s"  # heading
    r"|[-*+]\s"  # bullet list
    r"|\d+[.)]\s"  # ordered list
    r"|>\s?"  # blockquote
    r"|\|"  # table row
    r"|```|~~~"  # code fence
    r"|(?:-{3,}|\*{3,}|_{3,})\s*$"  # horizontal rule (---, ***, ___)
    r"|\[PAGE_\d+\]\s*$"  # synthetic page marker
    r")"
)

# A ``` / ~~~ fenced-code-block delimiter line (open or close).
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def dewrap_paragraphs(text: str) -> str:
    """Reflow PDF visual line-wraps so prose paragraphs sit on a single line.

    Backends like ``pymupdf4llm`` keep the PDF's line breaks inside a
    paragraph (e.g. ``"…assisted by the\\nexternal auditors…"``). Those
    mid-sentence ``\\n`` make the splitter break mid-sentence and read poorly
    once stored. We join consecutive prose lines with a space while preserving
    paragraph breaks (blank lines) and any markdown block line (heading, list
    item, table row, code fence, blockquote, rule, page marker), which keep
    their own line. See #579.
    """
    out: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if paragraph:
            out.append(" ".join(line.strip() for line in paragraph))
            paragraph.clear()

    for line in text.split("\n"):
        # Inside a ``` / ~~~ code fence, emit lines verbatim — never reflow, or
        # we'd destroy code indentation and line breaks.
        if _CODE_FENCE_RE.match(line):
            flush()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if line.strip() == "":
            flush()
            out.append("")
        elif _BLOCK_LINE_RE.match(line):
            flush()
            out.append(line)
        else:
            paragraph.append(line)
    flush()

    # Collapse any 3+ newline runs the flushing may have produced.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


class BaseChunker(ChunkingStrategy):
    """Base markdown-aware chunker.

    Subclasses must set ``self.text_splitter`` to an object with a
    ``.split_text(str) -> list[str]`` method (e.g. LangChain's
    ``RecursiveCharacterTextSplitter``).
    """

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        length_function: Callable[[str], int] | None = None,
        **kwargs: Any,
    ) -> None:
        if length_function is None:
            raise ValueError("length_function is required (e.g. tokenizer.count_tokens)")
        self.chunk_size = chunk_size
        self.chunk_overlap_rate = chunk_overlap_rate
        self.chunk_overlap = int(self.chunk_size * self.chunk_overlap_rate)
        self.length_function = length_function
        self.text_splitter: Any = None

    # ------------------------------------------------------------------
    # ChunkingStrategy contract
    # ------------------------------------------------------------------
    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        """Split a processed document into ``Chunk`` objects."""
        content = self._content_from(document)
        if not content.strip():
            return []

        metadata = self._chunk_metadata_base(document, partition)

        # BM25 and the embedder read ``text``, and ``text`` is all the answering
        # model gets: a name left in metadata cannot be found by a query like
        # "what is report.pdf about?". No page here, unlike structured_section:
        # this chunker's text chunks can carry the wrong page (#1204).
        header = source_header(
            str(metadata.get("filename") or metadata.get("source") or ""), self.chunk_size, self.length_function
        )
        # The header is embedded with the chunk, so it comes out of the budget.
        body_size = self.chunk_size - self.length_function(f"{header}\n\n") if header else self.chunk_size
        md_chunks = self._get_chunks(content=content.strip(), metadata=metadata, body_size=body_size)

        chunks: list[Chunk] = []
        for i, md_chunks_meta in enumerate(md_chunks):
            body = md_chunks_meta["page_content"]
            text = f"{header}\n\n{body}" if header else body
            chunks.append(
                Chunk(
                    document_id=metadata.get("file_id", ""),
                    text=text,
                    chunk_index=i,
                    chunk_type=ChunkType(md_chunks_meta["chunk_type"]),
                    metadata={
                        k: v for k, v in md_chunks_meta.items() if k not in ("page_content", "chunk_type", "page")
                    },
                    partition=partition,
                    page_number=md_chunks_meta.get("page"),
                    # The embedder-window checks read the stored count instead of
                    # re-tokenising every chunk on the event loop.
                    token_count=self.length_function(text),
                    header=header,
                    content=body if header else None,
                )
            )
        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _content_from(document: ProcessedDocument) -> str:
        """Reconstruct chunkable markdown from a ProcessedDocument.

        Single-block documents on page 1 (or with no page metadata) flow
        through unchanged. Anything else gets synthetic ``[PAGE_N]`` markers
        injected so downstream chunk-page resolution works correctly.

        Marker semantics: a ``[PAGE_N]`` marker means "everything BEFORE this
        marker was on page N" (see ``markdown_utils.get_page_number``). So we
        emit the marker for the *outgoing* page just before content from a
        new page begins, and we also prepend a marker for the first block if
        it doesn't start on page 1.

        Blocks are put in page order, keeping their relative order within a
        page: the caption stage appends a caption it has no position for after
        the last page, and the chunk list follows this order. A document with
        an unpaged block has nothing to sort on and keeps the order it came in.
        """
        if not document.text_blocks:
            return ""
        if len(document.text_blocks) == 1 and document.text_blocks[0].page_number in (None, 1):
            return document.text_blocks[0].text

        blocks = document.text_blocks
        if all(block.page_number is not None for block in blocks):
            blocks = sorted(blocks, key=lambda block: block.page_number)

        parts: list[str] = []
        last_page: int | None = None
        for index, block in enumerate(blocks):
            if block.page_number is not None:
                # Emit `[PAGE_{block.page_number - 1}]` immediately *before*
                # this block's text so downstream resolution lands on
                # block.page_number. Using `block.page_number - 1` (rather
                # than `last_page`) handles non-sequential pages (1 -> 5)
                # and a first block already on page > 1.
                needs_marker = (
                    (index == 0 and block.page_number > 1)
                    or (last_page is not None and block.page_number != last_page)
                    or (last_page is None and index > 0)
                )
                if needs_marker:
                    parts.append(f"[PAGE_{block.page_number - 1}]")
            parts.append(block.text)
            if block.page_number is not None:
                last_page = block.page_number
        return "\n\n".join(parts)

    @staticmethod
    def _chunk_metadata_base(document: ProcessedDocument, partition: str) -> dict[str, Any]:
        # Reserved identity fields must win — `chunk()` later reads
        # metadata["file_id"] to set Chunk.document_id, so a stray key in
        # `document.metadata` would silently reassign chunks to the wrong doc.
        return {
            **document.metadata,
            "file_id": document.document_id,
            "partition": partition,
        }

    def _build_text_splitter(self, chunk_size: int) -> Any:
        """A splitter for *chunk_size*, with this chunker's overlap rate."""
        from langchain.text_splitter import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=int(chunk_size * self.chunk_overlap_rate),
            length_function=self.length_function,
        )

    def split_text(self, text: str, chunk_size: int | None = None) -> list[str]:
        """Split a text string with the configured text splitter.

        Lazy-initializes a ``RecursiveCharacterTextSplitter`` if a subclass
        forgot to set one, which preserves legacy behavior. A *chunk_size* other than
        the configured one gets its own splitter: chunkers are shared across
        threads, so the configured one is never resized.
        """
        if chunk_size is not None and chunk_size != self.chunk_size:
            return self._build_text_splitter(chunk_size).split_text(text)
        if self.text_splitter is None:
            self.text_splitter = self._build_text_splitter(self.chunk_size)
        return self.text_splitter.split_text(text)

    def _prepare_md_elements(self, content: str) -> list[MDElement]:
        """Return the document's markdown elements in source order.

        Images are normalized in place: a placeholder-only caption block is
        dropped, real caption text keeps but without the marker. The list is
        strictly ordered (#353) — ``_get_chunks`` walks it in a single pass,
        so inline-vs-standalone is decided per element there rather than by
        bucketing, which would lose the position of each element in the
        document and scramble the emitted chunk order.
        """
        elements: list[MDElement] = []
        for element in split_md_elements(content):
            if element.type == "image":
                if is_placeholder_image(element.content):
                    continue
                element.content = strip_placeholder_markers(element.content)
            elements.append(element)
        return elements

    def _get_chunks(self, content: str, metadata: dict[str, Any], body_size: int | None = None) -> list[dict[str, Any]]:
        """Produce per-chunk dicts with ``page_content`` + metadata fields.

        Elements are consumed in source order (#353): inline-able pieces
        accumulate in a text buffer that is flushed — sanitized, split, and
        emitted — right before each standalone table/image, so no prose is
        ever joined across a standalone element and the list this returns
        IS the document's reading order (``chunk()`` numbers ``chunk_index``
        from it). The dict shape is intentional — it lets ``chunk()`` build
        ``Chunk`` objects without leaking domain types into the lower-level
        helpers. *body_size* is what each chunk's content may use, ``chunk_size``
        by default.
        """
        body_size = self.chunk_size if body_size is None else body_size
        elements = self._prepare_md_elements(content=content)
        # A document can flush hundreds of times, so its splitter is built once.
        split = self.split_text if body_size == self.chunk_size else self._build_text_splitter(body_size).split_text

        # Each chunk dict spreads `metadata` first, so reserved per-chunk keys
        # win over arbitrary `metadata` values — a stray "chunk_type" / "page" /
        # "page_content" in the document's metadata would otherwise clobber the
        # resolved value (and crash `chunk()` when ChunkType(...) is fed an
        # out-of-enum string). Same defensive pattern as `_chunk_metadata_base`.
        chunks: list[dict[str, Any]] = []
        buffer: list[str] = []  # dewrapped inline pieces awaiting the next flush
        prev_page = 1

        def flush_text() -> None:
            """Sanitize and split the buffered prose, emitting its chunks."""
            nonlocal prev_page
            if not buffer:
                return
            # Join the reflowed pieces as paragraphs (blank line) so the splitter
            # cuts on paragraph/sentence boundaries rather than mid-sentence
            # line-wraps (#579).
            combined_texts = "\n\n".join(buffer)
            buffer.clear()
            sanitized = sanitize_text(
                combined_texts,
                normalize_whitespace=True,
                remove_control_chars=True,
                remove_zero_width_chars=True,
                max_consecutive_newlines=2,
                normalize_unicode=True,
            )
            # Markers closing the buffer label nothing: the element that follows
            # sets `prev_page` itself. Left in, one could split off with the
            # replayed overlap, as a chunk with no new content.
            sanitized = _TRAILING_PAGE_MARKERS_RE.sub("", sanitized)
            # A piece between two pages can still hold nothing but markers. It
            # is dropped per piece, after `prev_page` has advanced through it.
            for text_chunk in split(sanitized):
                page_info = get_chunk_page_number(chunk_str=text_chunk, previous_chunk_ending_page=prev_page)
                prev_page = page_info["end_page"]
                if not PAGE_RE.sub("", text_chunk).strip():
                    continue
                chunks.append(
                    {
                        **metadata,
                        "page_content": text_chunk.strip(),
                        "page": page_info["start_page"],
                        "chunk_type": "text",
                    }
                )

        for element in elements:
            # Tables/images under the inline threshold join the surrounding
            # prose instead of becoming chunks of their own; only standalone
            # elements flush what came before and take their place in order.
            if element.type not in ("table", "image") or (
                self.length_function(element.content) <= _INLINE_ELEMENT_TOKEN_THRESHOLD
            ):
                buffer.append(dewrap_paragraphs(element.content))
                continue

            flush_text()
            if element.type == "table" and self.length_function(element.content) > body_size:
                if metadata.get("csv_columns"):
                    element.metadata = {key: value for key, value in metadata.items() if key.startswith("csv_")}
                subtables = chunk_table(
                    table_element=element,
                    chunk_size=body_size,
                    length_function=self.length_function,
                )
                chunks.extend(
                    {
                        **metadata,
                        **(subtable.metadata or {}),
                        "page_content": subtable.content.strip(),
                        "page": subtable.page_number,
                        "chunk_type": "table",
                    }
                    for subtable in subtables
                )
            else:
                # MDElement.type is the source-markdown literal ("image"/"table");
                # ChunkType uses "image_caption" for image blocks.
                ct = "image_caption" if element.type == "image" else element.type
                chunks.append(
                    {
                        **metadata,
                        "page_content": element.content.strip(),
                        "page": element.page_number,
                        "chunk_type": ct,
                    }
                )
            prev_page = element.page_number or prev_page

        flush_text()
        return chunks


@chunking_registry.register("recursive_splitter")
class RecursiveSplitter(BaseChunker):
    """Markdown-aware chunker backed by ``RecursiveCharacterTextSplitter``.

    Splits on paragraph boundaries first, then sentence terminators, then
    smaller separators.
    """

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        length_function: Callable[[str], int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            chunk_size=chunk_size,
            chunk_overlap_rate=chunk_overlap_rate,
            length_function=length_function,
            **kwargs,
        )
        self.text_splitter = self._build_text_splitter(self.chunk_size)

    def _build_text_splitter(self, chunk_size: int) -> Any:
        from langchain.text_splitter import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=int(chunk_size * self.chunk_overlap_rate),
            length_function=self.length_function,
            is_separator_regex=True,
            # Paragraph → sentence → line → word. A single "\n" is a last
            # resort (only when a sentence itself exceeds chunk_size), so the
            # splitter no longer breaks mid-sentence on a line-wrap (#579).
            separators=["\n\n", r"(?<=[\.\?\!])", "\n", " "],
        )
