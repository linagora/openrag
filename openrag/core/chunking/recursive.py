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
    MDElement,
    chunk_table,
    get_chunk_page_number,
    split_md_elements,
)
from core.chunking.registry import chunking_registry
from core.chunking.table_rows import chunk_table_legend, chunk_table_row
from core.models.chunk import Chunk, ChunkType
from core.models.document import ProcessedDocument, TextBlock
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
        metadata = self._chunk_metadata_base(document, partition)
        blocks = document.effective_text_blocks()
        if any(
            (block.block_type == "table_row" and block.table_row is not None)
            or (block.block_type == "table_legend" and block.table_legend is not None)
            for block in blocks
        ):
            md_chunks = self._get_block_aware_chunks(blocks, metadata)
        else:
            content = self._content_from_blocks(blocks)
            if not content.strip():
                return []
            md_chunks = self._get_chunks(content=content.strip(), metadata=metadata)

        return [
            Chunk(
                document_id=metadata.get("file_id", ""),
                text=md_chunks_meta["page_content"],
                chunk_index=i,
                chunk_type=ChunkType(md_chunks_meta["chunk_type"]),
                metadata={k: v for k, v in md_chunks_meta.items() if k not in ("page_content", "chunk_type", "page")},
                partition=partition,
                page_number=md_chunks_meta.get("page"),
            )
            for i, md_chunks_meta in enumerate(md_chunks)
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _content_from(document: ProcessedDocument) -> str:
        return BaseChunker._content_from_blocks(document.effective_text_blocks())

    @staticmethod
    def _content_from_blocks(blocks: list[TextBlock]) -> str:
        """Reconstruct chunkable markdown from a ProcessedDocument.

        Single-block documents on page 1 (or with no page metadata) flow
        through unchanged. Anything else gets synthetic ``[PAGE_N]`` markers
        injected so downstream chunk-page resolution works correctly.

        Marker semantics: a ``[PAGE_N]`` marker means "everything BEFORE this
        marker was on page N" (see ``markdown_utils.get_page_number``). So we
        emit the marker for the *outgoing* page just before content from a
        new page begins, and we also prepend a marker for the first block if
        it doesn't start on page 1.
        """
        if not blocks:
            return ""
        if len(blocks) == 1 and blocks[0].page_number in (None, 1):
            return blocks[0].text

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

    def _get_block_aware_chunks(
        self,
        blocks: list[TextBlock],
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        ordinary: list[TextBlock] = []

        def flush_ordinary() -> None:
            if not ordinary:
                return
            content = self._content_from_blocks(ordinary)
            if content.strip():
                chunks.extend(self._get_chunks(content.strip(), metadata))
            ordinary.clear()

        for block in blocks:
            if block.block_type == "table_legend" and block.table_legend is not None:
                flush_ordinary()
                chunks.extend(
                    {
                        **metadata,
                        **legend_chunk.metadata,
                        "page_content": legend_chunk.text,
                        "page": legend_chunk.page_number,
                        "chunk_type": "table",
                    }
                    for legend_chunk in chunk_table_legend(
                        block.table_legend,
                        chunk_size=self.chunk_size,
                        length_function=self.length_function,
                    )
                )
                continue
            if block.block_type != "table_row" or block.table_row is None:
                ordinary.append(block)
                continue
            flush_ordinary()
            chunks.extend(
                {
                    **metadata,
                    **row_chunk.metadata,
                    "page_content": row_chunk.text,
                    "page": row_chunk.page_number,
                    "chunk_type": "table",
                }
                for row_chunk in chunk_table_row(
                    block.table_row,
                    chunk_size=self.chunk_size,
                    length_function=self.length_function,
                )
            )
        flush_ordinary()
        return chunks

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

    def split_text(self, text: str) -> list[str]:
        """Split a text string with the configured text splitter.

        Lazy-initializes a ``RecursiveCharacterTextSplitter`` if a subclass
        forgot to set one — preserves legacy behavior.
        """
        if self.text_splitter is None:
            from langchain.text_splitter import RecursiveCharacterTextSplitter

            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                length_function=self.length_function,
            )
        return self.text_splitter.split_text(text)

    def _prepare_md_elements(self, content: str) -> tuple[list[MDElement], list[MDElement]]:
        """Separate markdown into (inline-able texts) and (standalone tables/images)."""
        md_elements = split_md_elements(content)
        tables_and_images: list[MDElement] = []
        texts: list[MDElement] = []

        for element in md_elements:
            if element.type in ("table", "image"):
                if element.type == "image":
                    if is_placeholder_image(element.content):
                        continue
                    element.content = strip_placeholder_markers(element.content)
                if self.length_function(element.content) <= _INLINE_ELEMENT_TOKEN_THRESHOLD:
                    texts.append(element)
                else:
                    tables_and_images.append(element)
            else:
                texts.append(element)

        return texts, tables_and_images

    def _get_chunks(self, content: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Produce per-chunk dicts with ``page_content`` + metadata fields.

        The dict shape is intentional — it lets ``chunk()`` build ``Chunk``
        objects without leaking domain types into the lower-level helpers.
        """
        texts, tables_and_images = self._prepare_md_elements(content=content)
        # Reflow PDF line-wraps within each element, then join elements as
        # paragraphs (blank line) so the splitter cuts on paragraph/sentence
        # boundaries rather than mid-sentence line-wraps (#579).
        combined_texts = "\n\n".join(dewrap_paragraphs(e.content) for e in texts)

        sanitized = sanitize_text(
            combined_texts,
            normalize_whitespace=True,
            remove_control_chars=True,
            remove_zero_width_chars=True,
            max_consecutive_newlines=2,
            normalize_unicode=True,
        )
        text_chunks = self.split_text(sanitized)

        chunks: list[dict[str, Any]] = []

        # Reserved per-chunk keys must win over arbitrary `metadata` values —
        # a stray "chunk_type" / "page" / "page_content" in the document's
        # metadata would otherwise clobber the resolved value (and crash
        # `chunk()` when ChunkType(...) is fed an out-of-enum string). Same
        # defensive pattern as `_chunk_metadata_base`.
        for element in tables_and_images:
            if element.type == "table" and self.length_function(element.content) > self.chunk_size:
                subtables = chunk_table(
                    table_element=element,
                    chunk_size=self.chunk_size,
                    length_function=self.length_function,
                )
                chunks.extend(
                    {
                        **metadata,
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

        prev_page = 1
        for c in text_chunks:
            page_info = get_chunk_page_number(chunk_str=c, previous_chunk_ending_page=prev_page)
            prev_page = page_info["end_page"]
            chunks.append(
                {
                    **metadata,
                    "page_content": c.strip(),
                    "page": page_info["start_page"],
                    "chunk_type": "text",
                }
            )

        if not chunks:
            return []
        chunks.sort(key=lambda d: d.get("page") or 0)
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
        from langchain.text_splitter import RecursiveCharacterTextSplitter

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=self.length_function,
            is_separator_regex=True,
            # Paragraph → sentence → line → word. A single "\n" is a last
            # resort (only when a sentence itself exceeds chunk_size), so the
            # splitter no longer breaks mid-sentence on a line-wrap (#579).
            separators=["\n\n", r"(?<=[\.\?\!])", "\n", " "],
        )
