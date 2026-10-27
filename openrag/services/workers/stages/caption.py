from __future__ import annotations

import asyncio
import re
from collections.abc import MutableMapping
from contextlib import nullcontext
from typing import Any

from core.models.document import ImageBlock, ProcessedDocument, TextBlock
from core.prompts.vlm_prompt_builder import wrap_caption
from core.vlm.vlm import VLM
from services.inference.runtime import get_vlm_semaphore
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials, stage_timeout
from tqdm.asyncio import tqdm

_PADDING_RE = re.compile(r"[ \t]{2,}")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDENTED_CODE_RE = re.compile(r"^(?: {4}| {0,3}\t)")
_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-+:?")
_BLOCK_START_RE = re.compile(r"^ {0,3}(?:>|#{1,6}(?:[ \t]|$)|(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$))")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_HTML_BLOCK_START_RE = re.compile(
    r"^ {0,3}(?:<(?:!--|\?|!\[CDATA\[|![A-Z])"
    r"|</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|"
    r"dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|"
    r"legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|pre|script|section|source|"
    r"style|summary|table|tbody|td|textarea|tfoot|th|thead|title|tr|track|ul)(?=[ \t/>]|$)"
    r"|</?[A-Za-z][^<>]*>[ \t]*$)",
    re.IGNORECASE,
)


def _pipe_table_cells(line: str) -> list[str] | None:
    """Return cells for a pipe-containing row, allowing optional outer pipes."""
    stripped = line.strip()
    cells = re.split(r"(?<!\\)\|", stripped)
    if len(cells) == 1:
        return None
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def squeeze_padding(caption: str) -> str:
    """Collapse padding in plain Markdown table captions only.

    Captions of tables come back as markdown tables padded for alignment, and a
    degenerate one can spend its whole token budget repeating padded rows. One
    space-run token can stand for many bytes, so such a caption is far larger
    than its token count suggests. Fenced code, indented lines, and table rows
    containing inline code keep their original spacing.
    """
    lines = caption.splitlines(keepends=True)
    contents = [line.rstrip("\r\n") for line in lines]
    parsed_rows = [_pipe_table_cells(content) for content in contents]
    delimiter_rows = {
        index
        for index, cells in enumerate(parsed_rows)
        if cells is not None and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in cells)
    }
    hard_protected_rows: set[int] = set()
    inline_code_rows: set[int] = set()
    fence_char: str | None = None
    fence_length = 0

    for index, content in enumerate(contents):
        fence_match = _FENCE_RE.match(content)
        if fence_char is not None:
            hard_protected_rows.add(index)
            if fence_match:
                marker = fence_match.group(1)
                trailing = content[fence_match.end() :]
                if marker[0] == fence_char and len(marker) >= fence_length and not trailing.strip(" \t"):
                    fence_char = None
            continue
        if fence_match:
            hard_protected_rows.add(index)
            marker = fence_match.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
        elif _INDENTED_CODE_RE.match(content):
            hard_protected_rows.add(index)
        elif index not in delimiter_rows and (
            _BLOCK_START_RE.match(content)
            or _THEMATIC_BREAK_RE.fullmatch(content)
            or _HTML_BLOCK_START_RE.match(content)
        ):
            hard_protected_rows.add(index)
        elif "`" in content:
            inline_code_rows.add(index)

    table_rows: set[int] = set()

    # Only the header must match the delimiter's width. Scan each table body
    # once, allowing missing or extra cells without touching preceding prose.
    index = 1
    while index < len(contents):
        cells = parsed_rows[index]
        header = index - 1
        header_cells = parsed_rows[header]
        if (
            index in hard_protected_rows
            or header in hard_protected_rows
            or cells is None
            or header_cells is None
            or len(header_cells) != len(cells)
            or index not in delimiter_rows
        ):
            index += 1
            continue
        if header not in inline_code_rows:
            table_rows.add(header)
        table_rows.add(index)
        index += 1
        while index < len(contents) and index not in hard_protected_rows and contents[index].strip():
            if index not in inline_code_rows:
                table_rows.add(index)
            index += 1

    # Some VLM table captions omit the delimiter row and repeat a padded row
    # many times. Preserve that optimization only for repeated multi-cell rows;
    # a standalone pipe diagram is left untouched.
    index = 0
    while index < len(contents):
        if index in hard_protected_rows or index in inline_code_rows:
            index += 1
            continue
        cells = parsed_rows[index]
        if cells is None or len(cells) < 2 or all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in cells):
            index += 1
            continue

        normalized = tuple(_PADDING_RE.sub(" ", cell).strip() for cell in cells)
        end = index + 1
        while end < len(contents):
            if end in hard_protected_rows or end in inline_code_rows:
                break
            next_cells = parsed_rows[end]
            if next_cells is None or len(next_cells) != len(cells):
                break
            if all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in next_cells):
                break
            next_normalized = tuple(_PADDING_RE.sub(" ", cell).strip() for cell in next_cells)
            if next_normalized != normalized:
                break
            end += 1
        if end - index >= 2:
            table_rows.update(range(index, end))
        index = max(end, index + 1)

    squeezed = [_PADDING_RE.sub(" ", line) if index in table_rows else line for index, line in enumerate(lines)]

    return "".join(squeezed)


async def caption_stage(
    row: MutableMapping[str, Any],
    vlm: VLM,
    *,
    timeout: float | None = None,
    per_image_timeout: float = 0.0,
    max_concurrency: int | None = None,
) -> MutableMapping[str, Any]:
    """Caption images in ``row["processed_document"]`` and mutate the row."""

    try:
        processed_document = row.get("processed_document")
        if not isinstance(processed_document, ProcessedDocument):
            raise ValueError("caption_stage row must contain a ProcessedDocument under 'processed_document'")

        prompt = row.get("caption_prompt")
        if prompt is not None:
            prompt = str(prompt)

        effective_timeout = stage_timeout(timeout, len(processed_document.images), per_item_timeout=per_image_timeout)
        row["processed_document"] = await run_with_optional_timeout(
            lambda: _caption_document(processed_document, vlm, prompt, max_concurrency),
            effective_timeout,
        )
        row["stage"] = "captioned"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "caption_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


async def _caption_document(
    processed_document: ProcessedDocument,
    vlm: VLM,
    prompt: str | None,
    max_concurrency: int | None,
) -> ProcessedDocument:
    """Return a copy of ``processed_document`` with captions materialized.

    ``max_concurrency`` has no default on purpose. It defaulted to ``None`` when
    the cap was introduced, and the second caller — the file-serializer path
    behind the extractText tool and ``/extract`` — silently kept fanning out
    unbounded because omitting the argument was legal. Requiring it makes a new
    call site fail loudly instead of quietly uncapped; ``None`` is still how a
    caller asks for no bound, it just has to ask.
    """
    images = processed_document.images
    if not images:
        return processed_document

    # Per-document fan-out bound. One task per image is created either way, but
    # only this many contend for the shared VLM gate at a time. Without it a
    # single image-heavy document queues hundreds of its own callers on that
    # gate, and the gate's wait bound stops meaning anything for that document:
    # its last images would time out waiting behind their own siblings on a
    # perfectly healthy endpoint.
    limiter = (
        asyncio.Semaphore(max_concurrency) if max_concurrency is not None and max_concurrency > 0 else nullcontext()
    )

    async def caption_one(image: ImageBlock) -> str:
        # Bound the fan-out cluster-wide so every indexer actor shares one VLM budget.
        async with limiter, get_vlm_semaphore():
            return await vlm.caption_image(image.image_bytes, prompt=prompt)

    # tqdm.gather is built on as_completed, so a failing child does not clean up
    # sibling tasks for us. Drain them explicitly so failed documents do not keep
    # using the shared VLM semaphore in the background.
    label = processed_document.metadata.get("filename") or processed_document.document_id
    tasks = [asyncio.ensure_future(caption_one(image)) for image in images]
    try:
        captions = await tqdm.gather(*tasks, desc=f"Captioning images of *{label}*")
    except asyncio.CancelledError:
        await _cancel_and_drain(tasks)
        raise
    except Exception:
        await _cancel_and_drain(tasks)
        raise

    # Caption fetching fans out concurrently above; the text-block mutation
    # below stays sequential and in image order so the markdown-ref replacement
    # and append ordering are deterministic.
    text_blocks = list(processed_document.text_blocks)
    captioned_images: list[ImageBlock] = []
    for image, raw_caption in zip(images, captions, strict=True):
        caption = squeeze_padding(raw_caption)
        wrapped = wrap_caption(caption)
        captioned_images.append(image.model_copy(update={"caption": caption}))
        if not _replace_markdown_ref(text_blocks, image, wrapped):
            text_blocks.append(TextBlock(text=wrapped, page_number=image.page_number))

    return processed_document.model_copy(
        update={
            "text_blocks": text_blocks,
            "images": captioned_images,
        }
    )


async def _cancel_and_drain(tasks: list[asyncio.Task[str]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _replace_markdown_ref(text_blocks: list[TextBlock], image: ImageBlock, wrapped_caption: str) -> bool:
    """Replace an image markdown placeholder in text blocks when one exists."""
    markdown_ref = image.metadata.get("markdown_ref")
    if not markdown_ref:
        return False

    replaced = False
    for index, block in enumerate(text_blocks):
        if markdown_ref not in block.text:
            continue
        text_blocks[index] = block.model_copy(update={"text": block.text.replace(markdown_ref, wrapped_caption)})
        replaced = True
    return replaced
