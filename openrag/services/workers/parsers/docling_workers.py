"""Docling-backed PDF Ray actors and BasePooledParser facade.

``DoclingWorker`` and ``DoclingPool`` are Ray actors.
``DoclingLoader`` is a :class:`~core.indexing.parsers.document_parser.BasePooledParser`
that wraps the pool so the core pipeline can call ``parse()`` uniformly.

The old module ``components/indexer/loaders/pdf_loaders/docling2.py`` re-exports
``DoclingWorker`` and ``DoclingPool`` for legacy import paths.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Mapping
from collections.abc import Set as AbstractSet
from typing import Any

import ray
import torch
from core.config import load_config
from core.indexing.image_preprocessor import decode_data_uri
from core.indexing.parsers.document_parser import BasePooledParser
from core.indexing.parsers.progress import report_parser_substage
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from core.utils.logging import get_logger
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.serializer.base import BaseDocSerializer, SerializationResult
from docling_core.transforms.serializer.markdown import (
    MarkdownDocSerializer,
    MarkdownParams,
    MarkdownPictureSerializer,
)
from docling_core.types.doc.document import (
    ContentLayer,
    DocItem,
    DoclingDocument,
    FloatingItem,
    FormItem,
    GroupItem,
    InlineGroup,
    KeyValueItem,
    ListGroup,
    NodeItem,
    PictureItem,
    PictureTabularChartData,
    TableItem,
    TextItem,
)
from pydantic import PrivateAttr

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff

logger = get_logger()

# What docling-core prints where a picture's image goes (``image_placeholder``).
# ``_PictureRefSerializer`` swaps it for the picture's own ref before the page
# text is assembled, so it never reaches the page text.
_IMAGE_PLACEHOLDER = "<!-- image -->"


def _docling_num_gpus(config) -> float:
    """Return Docling's Ray GPU reservation, falling back to CUDA detection.

    Must mirror Marker: ``_docling_num_gpus`` runs inside the ``DoclingPool``
    actor, which Ray schedules with no GPU — so ``torch.cuda.is_available()`` is
    False there (``CUDA_VISIBLE_DEVICES=""``) and a naive local check reserves 0
    GPUs for the worker, leaving Docling stuck on CPU. Query the Ray *cluster*
    for GPU capacity instead, with local CUDA only as a startup fallback.
    """
    requested_gpus = config.loader.docling_num_gpus
    if requested_gpus <= 0:
        return 0
    try:
        return requested_gpus if ray.cluster_resources().get("GPU", 0) > 0 else 0
    except Exception as exc:
        logger.warning("Failed to query Ray cluster GPU resources; falling back to CUDA check", error=str(exc))
        return requested_gpus if torch.cuda.is_available() else 0


class _PictureRefSerializer(MarkdownPictureSerializer):
    """Prints a picture as the ``markdown_ref`` of the ``ImageBlock`` built from it.

    ``refs`` maps a picture's ``self_ref`` to that ref, so the caption stage finds
    each picture's ref exactly where the picture is, whatever else the page
    holds (#1313). A picture with no block, its image unreadable, prints nothing
    in its image's place; its caption, if any, stays.
    """

    def __init__(self, refs: Mapping[str, str]) -> None:
        self._refs = refs

    def serialize(
        self,
        *,
        item: PictureItem,
        doc_serializer: BaseDocSerializer,
        doc: DoclingDocument,
        **kwargs: Any,
    ) -> SerializationResult:
        result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
        # docling-core joins a picture's caption, image and chart table with a
        # blank line, the image part being the placeholder alone.
        ref = self._refs.get(item.self_ref, "")
        parts = [ref if part == _IMAGE_PLACEHOLDER else part for part in result.text.split("\n\n")]
        return result.model_copy(update={"text": "\n\n".join(part for part in parts if part)})


class _ExcludedRefs(AbstractSet[str]):
    """``get_excluded_refs`` for one set of pages, answered from a ``_PageIndex``."""

    def __init__(self, always: AbstractSet[str], page_of: Mapping[str, int | None], pages: AbstractSet[int]) -> None:
        self._always = always
        self._page_of = page_of
        self._pages = pages

    def __contains__(self, ref: object) -> bool:
        if ref in self._always:
            return True
        # A DocItem with no provenance has page None, which no page set holds.
        return ref in self._page_of and self._page_of[ref] not in self._pages

    def __iter__(self) -> Iterator[str]:
        yield from self._always
        yield from (ref for ref, page in self._page_of.items() if page not in self._pages)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _PageMarkdownSerializer(MarkdownDocSerializer):
    """``MarkdownDocSerializer`` for one page that reads its whole-document
    lookups, the excluded refs and the caption refs, from a ``_PageIndex``.

    The base class rebuilds both lookups by walking the whole document once per
    instance, which is once per page. ``index`` is required, so every instance
    has one; ``model_copy`` keeps it.
    """

    _index: _PageIndex = PrivateAttr()

    def __init__(self, *, index: _PageIndex, **data: Any) -> None:
        super().__init__(**data)
        self._index = index

    @property
    def _captions_of_some_item(self) -> set[str]:
        return self._index.captions

    def get_excluded_refs(self, **kwargs: Any) -> AbstractSet[str]:
        params = self.params.merge_with_patch(patch=kwargs) if kwargs else self.params
        if params != self.params:
            return super().get_excluded_refs(**kwargs)
        return self._index.excluded_refs(params.pages)


class _PageIndex:
    """A document read once for exporting its pages one at a time (#1312).

    ``MarkdownDocSerializer`` limited to one page still serializes every item of
    the document to keep the ones on that page, and rescans the document for its
    lookups, so exporting each page cost pages × items. ``markdown`` serializes
    only the top-level subtrees that can print something on the page, in
    document order, with the same visits as the whole-document export; a subtree
    left out would print nothing on that page.

    A list or inline group is one subtree, serialized whole on every page it
    prints on, so a list spanning k pages costs k times its items. Splitting it
    per page would mean reimplementing docling-core's list serializer, which
    numbers the items across the whole list.
    """

    def __init__(self, document: DoclingDocument) -> None:
        self.document = document
        self.params = MarkdownParams(image_placeholder=_IMAGE_PLACEHOLDER)
        # Constructing a serializer validates ``document``, which walks the whole
        # tree; each page's serializer is a copy of this one, which does not.
        self._serializer = _PageMarkdownSerializer(doc=document, params=self.params, index=self)

        # The same lookups as ``DocSerializer._captions_of_some_item`` and
        # ``DocSerializer.get_excluded_refs``, the page condition kept apart.
        self.captions = {
            caption.cref
            for item, _ in document.iterate_items(
                with_groups=True, traverse_pictures=True, included_content_layers=set(ContentLayer)
            )
            if isinstance(item, FloatingItem)
            for caption in item.captions
        }
        params = self.params
        self._always_excluded: set[str] = set()
        self._page_of: dict[str, int | None] = {}
        for ix, (item, _) in enumerate(
            document.iterate_items(with_groups=True, traverse_pictures=True, included_content_layers=params.layers)
        ):
            if ix < params.start_idx or ix >= params.stop_idx:
                self._always_excluded.add(item.self_ref)
            elif isinstance(item, DocItem):
                if item.label not in params.labels or item.content_layer not in params.layers:
                    self._always_excluded.add(item.self_ref)
                else:
                    self._page_of[item.self_ref] = item.prov[0].page_no if item.prov else None

        self._subtrees = list(self._top_level_subtrees(document.body))
        self._subtrees_on_page: dict[int, list[int]] = {}
        self._subtrees_on_every_page: list[int] = []
        for position, subtree in enumerate(self._subtrees):
            pages = self._pages_printed_by(subtree)
            if pages is None:
                self._subtrees_on_every_page.append(position)
            else:
                for page_no in pages:
                    self._subtrees_on_page.setdefault(page_no, []).append(position)

    def excluded_refs(self, pages: AbstractSet[int] | None) -> AbstractSet[str]:
        if pages is None:
            return self._always_excluded
        return _ExcludedRefs(self._always_excluded, self._page_of, pages)

    def markdown(self, page_no: int, refs: Mapping[str, str]) -> str:
        """``document.export_to_markdown(page_no=page_no)``, each picture printed as its ref."""
        serializer = self._serializer.model_copy(
            update={
                "picture_serializer": _PictureRefSerializer(refs),
                "params": self.params.merge_with_patch(patch={"pages": {page_no}}),
            }
        )
        # What ``serializer.serialize()`` passes down to each item (``get_parts``).
        kwargs = serializer.params.model_dump()
        visited = {self.document.body.self_ref}
        parts: list[SerializationResult] = []
        positions = sorted({*self._subtrees_on_page.get(page_no, ()), *self._subtrees_on_every_page})
        for position in positions:
            for node, _ in self.document.iterate_items(
                root=self._subtrees[position], with_groups=True, included_content_layers=serializer.params.layers
            ):
                if node.self_ref in visited:
                    continue
                visited.add(node.self_ref)
                part = serializer.serialize(item=node, list_level=0, is_inline_scope=False, visited=visited, **kwargs)
                if part.text:
                    parts.append(part)
        return serializer.serialize_doc(parts=parts, **kwargs).text.strip()

    def _top_level_subtrees(self, group: NodeItem) -> Iterator[NodeItem]:
        """The subtrees the export visits from ``group``, in order. A plain group
        prints nothing and its children are visited one by one, so they are
        subtrees of their own; lists and inline groups print as a whole."""
        for ref in group.children:
            child = ref.resolve(self.document)
            if isinstance(child, GroupItem) and not isinstance(child, (ListGroup, InlineGroup)):
                yield from self._top_level_subtrees(child)
            elif isinstance(child, NodeItem):
                yield child

    def _pages_printed_by(self, subtree: NodeItem) -> set[int] | None:
        """The pages ``subtree`` can print anything on, or ``None`` for every page.

        More pages than it prints on only costs time. Captions print with their
        picture or table whatever page it is on. ``serialize_captions`` prints a
        caption on each page whose excluded refs lack it, so a caption neither
        lookup holds, such as one outside the body layer, prints on every page.
        A picture or table outside the exported layers is never serialized, so
        its captions and a chart's table print nowhere through it.
        """
        pages: set[int] = set()
        for node, _ in self.document.iterate_items(
            root=subtree, with_groups=True, traverse_pictures=True, included_content_layers=set(ContentLayer)
        ):
            if not isinstance(node, DocItem):
                continue
            if not isinstance(node, (TextItem, TableItem, PictureItem, KeyValueItem, FormItem)):
                return None  # the fallback serializer prints these on every page
            if (
                isinstance(node, PictureItem)
                and node.content_layer in self.params.layers
                and any(isinstance(annotation, PictureTabularChartData) for annotation in node.annotations)
            ):
                return None  # a chart's table prints on every page
            pages.update(prov.page_no for prov in node.prov)
            if isinstance(node, FloatingItem) and node.content_layer in self.params.layers:
                for ref in node.captions:
                    caption = ref.resolve(self.document)
                    if not isinstance(caption, DocItem):
                        continue
                    if (
                        isinstance(caption, TextItem)
                        and caption.self_ref not in self._always_excluded
                        and caption.self_ref not in self._page_of
                    ):
                        return None  # no page's excluded refs hold it
                    pages.update(prov.page_no for prov in caption.prov)
        return pages


def _page_markdown(
    document: DoclingDocument, page_no: int, refs: Mapping[str, str], index: _PageIndex | None = None
) -> str:
    """``document.export_to_markdown(page_no=page_no)``, each picture printed as its ref.

    ``index`` is ``_PageIndex(document)``. Building it reads the whole document,
    so a caller exporting every page builds it once.
    """
    return (index or _PageIndex(document)).markdown(page_no, refs)


@ray.remote
class DoclingWorker:
    def __init__(self):
        img_scale = 2
        pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=True,
            generate_picture_images=True,
            images_scale=img_scale,
        )
        pipeline_options.table_structure_options = TableStructureOptions(
            do_cell_matching=True, mode=TableFormerMode.ACCURATE
        )
        pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO)
        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend)
            }
        )

    async def convert(self, file_path: str) -> ConversionResult:
        with torch.no_grad():
            return await asyncio.to_thread(self.converter.convert, str(file_path))


# max_restarts mirrors MarkerPool: the detached pool supervisor must survive its
# own crashes rather than staying permanently dead (#722). Ray's default is 0.
@ray.remote(max_restarts=5)
class DoclingPool:
    def __init__(self):
        self.logger = get_logger()
        self.config = load_config()
        self.pool_size = self.config.loader.docling_pool_size
        self.max_tasks_per_worker = self.config.loader.docling_max_tasks_per_worker

        # max_restarts mirrors MarkerWorker: a worker that OOMs/CUDA-faults is
        # restarted in place (same actor id), so the handle already queued keeps
        # working instead of every retry drawing the same dead handle (#722).
        self.actors = [
            DoclingWorker.options(num_gpus=_docling_num_gpus(self.config), max_restarts=5).remote()
            for _ in range(self.pool_size)
        ]
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(self.max_tasks_per_worker):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        total_slots = self.pool_size * self.max_tasks_per_worker
        self.logger.info(
            f"Docling pool: {self.pool_size} actors × {self.max_tasks_per_worker} slots = {total_slots} PDF concurrency"
        )

    async def process_pdf(self, file_path: str) -> ConversionResult:
        timeout = self.config.loader.docling_timeout

        async def attempt(i: int) -> ConversionResult:
            actor: DoclingWorker = await self._queue.get()
            try:
                return await call_ray_actor_with_timeout(
                    actor.convert.remote(file_path),
                    timeout=timeout,
                    task_description=f"DoclingPool PDF ({file_path})",
                )
            finally:
                await self._queue.put(actor)

        return await retry_with_backoff(
            attempt,
            max_retries=self.config.loader.docling_max_task_retry,
            base_delay=self.config.loader.docling_retry_base_delay,
            task_description=f"DoclingPool PDF ({file_path})",
        )


class DoclingLoader(BasePooledParser):
    """``BasePooledParser`` facade over the Docling Ray pool.

    Materialises ``Document.raw_bytes`` to a temporary file, dispatches to the
    named ``DoclingPool`` actor, and converts the ``ConversionResult`` into a
    ``ProcessedDocument`` with one ``TextBlock`` per page and one ``ImageBlock``
    per picture.  Image captioning is left to the downstream caption stage:
    each picture is printed in its page's text as the ref of its block, and
    each block keeps its picture's page (``prov[0].page_no``), so the stage can
    land every caption where the figure was.
    """

    def __init__(self) -> None:
        self.config = load_config()

    def _pool(self) -> DoclingPool:
        """Look up the named ``DoclingPool`` actor; call once per dispatch.

        Never cached: this loader lives as long as its indexer worker, and
        ``POST /actors/DoclingPool/restart`` replaces the pool with a new actor,
        so a cached handle would keep dispatching to the killed one. Store the
        result before calling a method on it: Ray's ``ActorMethod`` holds its
        handle weakly, so ``self._pool().process_pdf.remote()`` raises
        "Lost reference to actor".
        """
        # Create the pool lazily if bootstrap didn't: bootstrap only pre-warms the
        # globally-configured PDF backend, but a per-preset parsing_strategy can
        # select docling even when the global default is marker (see #569/#575).
        from services.workers.bootstrap import get_or_create_actor

        return get_or_create_actor("DoclingPool", DoclingPool, lifetime="detached")

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        report_parser_substage("parse.convert", "Converting PDF with Docling")
        async with document.as_temporary_file() as path:
            result: ConversionResult = await self._dispatch(str(path))

        # Exporting each page re-walks the whole document: it can take minutes of
        # CPU on a large PDF. On the loop that starves the lease renewals of every
        # other file on this worker.
        # A timeout or cancel cannot interrupt the thread; the event tells it to stop.
        cancelled = threading.Event()
        try:
            text_blocks, image_blocks = await asyncio.to_thread(self._postprocess, result, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=image_blocks,
            metadata=dict(document.metadata),
            page_count=len(result.pages),
        )

    def _postprocess(
        self, result: ConversionResult, cancelled: threading.Event
    ) -> tuple[list[TextBlock], list[ImageBlock]]:
        """Build the text and image blocks off the loop.

        The image blocks are built first: the page text prints each picture as
        the ref of its block, looked up by the picture's ``self_ref``.

        ``cancelled`` is checked before each page and each picture: once set,
        the caller has gone and the result is discarded, so the work stops early.
        """
        blocks: list[ImageBlock] = []
        refs: dict[str, str] = {}
        page_refs: dict[int, list[str]] = {}
        for idx, picture in enumerate(result.document.pictures):
            if cancelled.is_set():
                break
            # Docling indexes pages from 1, like OpenRAG's TextBlock/Marker blocks.
            page_no = picture.prov[0].page_no if picture.prov else None
            block = self._build_image_block(picture, idx, page_no)
            if block is not None:
                ref = block.metadata["markdown_ref"]
                refs[picture.self_ref] = ref
                if page_no is not None:
                    page_refs.setdefault(page_no, []).append(ref)
                blocks.append(block)
        return self._build_text_blocks(result, refs, page_refs, cancelled), blocks

    @staticmethod
    def _build_image_block(picture: Any, idx: int, page_no: int | None) -> ImageBlock | None:
        """The ``ImageBlock`` of one picture, without encoding it; ``None`` if unreadable.

        Docling PNG-encoded each picture into its ``data:`` URI when it generated
        it, so those bytes are reused as they are. A picture without one keeps its
        decoded image in ``pil_image``, which the caption stage encodes only when
        it captions the document. A picture Docling generated no image for has
        no block, and nothing failed, so nothing is logged.
        """
        if picture.image is None:
            return None
        try:
            uri = str(getattr(picture.image, "uri", None) or "")
            png_bytes = decode_data_uri(uri) if uri.startswith("data:image/png;") else None
            pil_image = None if png_bytes else picture.image.pil_image
        except Exception as exc:
            logger.warning(f"Failed to read Docling picture {idx}: {exc}")
            return None
        if not png_bytes and pil_image is None:
            return None
        return ImageBlock(
            image_bytes=png_bytes or b"",
            pil_image=pil_image,
            page_number=page_no,
            mime_type="image/png",
            metadata={"markdown_ref": f"![](docling_img_{idx})"},
        )

    async def _dispatch(self, file_path: str) -> ConversionResult:
        pool = self._pool()
        return await call_ray_actor_with_timeout(
            pool.process_pdf.remote(file_path),
            timeout=self.config.loader.docling_timeout,
            task_description=f"DoclingLoader PDF loading ({file_path})",
        )

    @staticmethod
    def _build_text_blocks(
        result: ConversionResult,
        refs: Mapping[str, str] | None = None,
        page_refs: Mapping[int, list[str]] | None = None,
        cancelled: threading.Event | None = None,
    ) -> list[TextBlock]:
        """One ``TextBlock`` per page, each picture printed as its ref (``refs``).

        ``export_to_markdown`` would print ``<!-- image -->`` for every picture,
        which the caption stage never looks for: each caption was appended at
        the end of the document, attributed to the last page, and the marker
        was indexed (#1313).

        A picture the page export leaves out (outside the body layer, like a
        logo) has its ref put at the end of its page (``page_refs``): appended
        after the last page instead, its caption was chunked with that page's
        text, which then took the caption's page number.

        The pages are the ones the document holds, by their own number: a page
        Docling left out leaves a gap, so counting ``result.pages`` from 1
        would export a missing page and skip the last one.
        """
        blocks: list[TextBlock] = []
        page_numbers = sorted(result.document.pages)
        n_pages = len(page_numbers)
        refs = refs or {}
        page_refs = page_refs or {}
        if n_pages:
            report_parser_substage("parse.text", "Extracting page text", progress=0, total=n_pages)
        else:
            report_parser_substage("parse.text", "Extracting page text")
        index: _PageIndex | None = None
        for done, page_no in enumerate(page_numbers, start=1):
            if cancelled is not None and cancelled.is_set():
                break
            if index is None:
                # Built after the first check, so a parse cancelled before the
                # export starts never reads the whole document.
                index = _PageIndex(result.document)
                if cancelled is not None and cancelled.is_set():
                    break  # cancelled while the index was built
            text = _page_markdown(result.document, page_no, refs, index)
            left_out = [ref for ref in page_refs.get(page_no, ()) if ref not in text]
            text = "\n\n".join([text, *left_out]).strip()
            blocks.append(TextBlock(text=text, page_number=page_no))
            report_parser_substage("parse.text", "Extracting page text", progress=done, total=n_pages)
        return blocks


__all__ = ["DoclingLoader", "DoclingPool", "DoclingWorker"]
