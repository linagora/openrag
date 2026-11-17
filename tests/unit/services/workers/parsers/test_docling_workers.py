from __future__ import annotations

import asyncio
import base64
import io
import itertools
import random
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from core.chunking.recursive import RecursiveSplitter
from core.chunking.structured_section import StructuredSectionChunker
from core.models.document import Document, DocumentType, ProcessedDocument
from docling_core.transforms.serializer.common import DocSerializer
from docling_core.transforms.serializer.markdown import MarkdownDocSerializer, MarkdownParams
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import (
    ContentLayer,
    DoclingDocument,
    GraphData,
    ImageRef,
    NodeItem,
    PictureItem,
    PictureTabularChartData,
    ProvenanceItem,
    TableCell,
    TableData,
    TextItem,
)
from docling_core.types.doc.labels import DocItemLabel, GroupLabel
from PIL import Image
from services.workers.parsers import docling_workers
from services.workers.stages import caption as caption_module
from services.workers.stages.caption import caption_stage, drop_uncaptioned_image_refs


def _config(docling_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Docling GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(docling_num_gpus=docling_num_gpus))


def test_docling_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Docling must request a GPU from Ray even when local CUDA is hidden in the
    pool process (CUDA_VISIBLE_DEVICES=""), otherwise it silently parses on CPU."""
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config()) == 0.25


def test_docling_num_gpus_zero_when_cluster_has_no_gpu(monkeypatch):
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", dict)

    assert docling_workers._docling_num_gpus(_config()) == 0


def test_docling_num_gpus_zero_when_not_requested(monkeypatch):
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config(docling_num_gpus=0)) == 0


# --- #722: the detached pool supervisor must survive its own crashes ----------


def test_docling_pool_sets_max_restarts():
    """DoclingPool had a bare @ray.remote — Ray's default max_restarts is 0, so
    one crash left the detached actor permanently dead and every docling parse
    afterwards failed. It must be restartable like MarkerPool/WhisperActor."""
    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == 5


def test_docling_pool_restart_policy_matches_marker():
    """Parity guard: if MarkerPool's policy changes, this flags that DoclingPool
    should be reconsidered too, rather than silently drifting apart."""
    from services.workers.parsers.marker_workers import MarkerPool

    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == MarkerPool.__ray_metadata__.max_restarts


# ---------------------------------------------------------------------------
# DoclingLoader.parse keeps the event loop free while building the blocks
# ---------------------------------------------------------------------------


def _png_data_uri(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode()


def _fake_conversion(n_pages: int, n_pictures: int):
    """The slice of a ``ConversionResult`` that the block builders read."""
    document = _docling_document(n_pages)
    for _ in range(n_pictures):
        _add_picture(document, 1)
    return SimpleNamespace(pages=[None] * n_pages, document=document)


def _pdf() -> Document:
    return Document(id="d", filename="f.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF")


async def test_parse_builds_blocks_off_the_event_loop(monkeypatch):
    """Exporting the pages of a large PDF must not starve other coroutines
    (worker lease renewals) running on the same loop."""
    loader = docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)

    ticks = 0
    # Ticks seen when the export starts and ends: only that span counts, not the
    # temporary-file write around the dispatch.
    window: list[int] = []

    def _slow_export(*_args):
        window.append(ticks)
        time.sleep(0.5)
        window.append(ticks)
        return "text"

    async def _fake_dispatch(path):
        return _fake_conversion(n_pages=1, n_pictures=0)

    monkeypatch.setattr(loader, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(docling_workers, "_page_markdown", _slow_export)

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(_ticker())
    try:
        parsed = await loader.parse(_pdf())
    finally:
        task.cancel()

    during_export = window[1] - window[0]
    assert during_export >= 20, f"event loop was blocked while exporting the pages (ticks={during_export})"
    assert [block.text for block in parsed.text_blocks] == ["text"]


@pytest.mark.parametrize("slow_step", ["page export", "picture decoding"])
async def test_cancelling_parse_stops_building_the_blocks(monkeypatch, slow_step):
    """A cancelled parse must not leave the worker thread exporting every
    remaining page or decoding every remaining picture."""
    loader = docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)
    total = 50
    done = 0
    started = threading.Event()

    def _slow(value):
        def _step(*_args, **_kwargs):
            nonlocal done
            done += 1
            started.set()
            time.sleep(0.02)
            return value

        return _step

    if slow_step == "page export":
        result = _fake_conversion(n_pages=total, n_pictures=0)
        monkeypatch.setattr(docling_workers, "_page_markdown", _slow("text"))
    else:
        result = _fake_conversion(n_pages=1, n_pictures=total)
        monkeypatch.setattr(docling_workers, "decode_data_uri", _slow(b"png"))

    async def _fake_dispatch(path):
        return result

    monkeypatch.setattr(loader, "_dispatch", _fake_dispatch)

    task = asyncio.create_task(loader.parse(_pdf()))
    deadline = time.monotonic() + 5
    while not started.is_set():
        if task.done():
            await task  # surfaces the parse error, if any
            pytest.fail(f"parse returned before its {slow_step} started")
        assert time.monotonic() < deadline, f"{slow_step} never started"
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.1)  # let the thread reach its next check
    settled = done
    await asyncio.sleep(0.3)
    assert done == settled < total


def test_a_parse_cancelled_before_the_page_export_does_not_index_the_document(monkeypatch):
    """Cancelled while its pictures decode, a parse reaches the page export with
    the event already set, and stops there without reading the whole document."""

    def _no_index(_document):
        raise AssertionError("the page index was built for a cancelled parse")

    monkeypatch.setattr(docling_workers, "_PageIndex", _no_index)
    cancelled = threading.Event()
    cancelled.set()
    result = SimpleNamespace(document=_docling_document(3))
    assert docling_workers.DoclingLoader._build_text_blocks(result, cancelled=cancelled) == []


# ----------------------------------------------------------------------------------
# #1313: the caption stage substitutes a caption for the picture's ``markdown_ref``
# wherever it occurs in the page text. ``export_to_markdown`` printed
# ``<!-- image -->`` instead, so every caption was appended at the end of the
# document and attributed to the last page, and the marker itself was indexed.
# These build real docling-core documents: which pictures a page export prints
# (body layer only, ``prov[0]`` on that page) is docling-core's call.
# ----------------------------------------------------------------------------------


def _docling_document(n_pages: int = 3) -> DoclingDocument:
    document = DoclingDocument(name="report")
    for page_no in range(1, n_pages + 1):
        document.add_page(page_no=page_no, size=Size(width=100, height=100))
    return document


def _prov(page_no: int) -> ProvenanceItem:
    return ProvenanceItem(page_no=page_no, bbox=BoundingBox(l=0, t=0, r=10, b=10), charspan=(0, 0))


def _add_text(document: DoclingDocument, page_no: int, text: str, label=DocItemLabel.TEXT):
    return document.add_text(label=label, text=text, prov=_prov(page_no))


def _add_picture(document: DoclingDocument, page_no: int | None, *, width: int = 2, **kwargs) -> PictureItem:
    """A picture carrying the PNG data URI Docling generates for it."""
    image = ImageRef.from_pil(Image.new("RGB", (width, 2)), dpi=72)
    return document.add_picture(image=image, prov=_prov(page_no) if page_no else None, **kwargs)


def _postprocess(document: DoclingDocument):
    loader = docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)
    result = SimpleNamespace(pages=[None] * len(document.pages), document=document)
    return loader._postprocess(result, threading.Event())


def test_each_picture_is_printed_as_the_ref_of_its_own_block():
    document = _docling_document()
    _add_text(document, 1, "Before")
    first = _add_picture(document, 1)
    caption = _add_text(document, 1, "Figure 1: revenue", label=DocItemLabel.CAPTION)
    first.captions.append(caption.get_ref())
    _add_text(document, 1, "Middle")
    _add_picture(document, 1)
    _add_text(document, 3, "Last page")
    _add_picture(document, 3)

    text_blocks, image_blocks = _postprocess(document)

    assert [(b.page_number, b.text) for b in text_blocks] == [
        (1, "Before\n\nFigure 1: revenue\n\n![](docling_img_0)\n\nMiddle\n\n![](docling_img_1)"),
        (2, ""),
        (3, "Last page\n\n![](docling_img_2)"),
    ]
    assert [b.metadata["markdown_ref"] for b in image_blocks] == [f"![](docling_img_{i})" for i in range(3)]
    assert [b.page_number for b in image_blocks] == [1, 1, 3]


def test_a_furniture_picture_does_not_stop_the_body_pictures_of_its_page_anchoring():
    """The page export leaves out a picture outside the body layer (a logo):
    counting it among the page's pictures disabled every ref of the page."""
    document = _docling_document(n_pages=1)
    _add_picture(document, 1, content_layer=ContentLayer.FURNITURE)
    _add_text(document, 1, "Body")
    _add_picture(document, 1)

    text_blocks, _ = _postprocess(document)

    assert text_blocks[0].text.startswith("Body\n\n![](docling_img_1)")


def test_a_picture_the_page_export_leaves_out_ends_its_own_page():
    """Not on the last page: appended after it, its caption was chunked with
    the last page's text, which took the caption's page number."""
    document = _docling_document()
    _add_text(document, 1, "First page")
    _add_picture(document, 1, content_layer=ContentLayer.FURNITURE)
    _add_text(document, 3, "Last page")

    text_blocks, image_blocks = _postprocess(document)

    assert [b.page_number for b in image_blocks] == [1]
    assert [b.text for b in text_blocks] == ["First page\n\n![](docling_img_0)", "", "Last page"]


def test_a_picture_without_provenance_has_no_page_and_does_not_shift_the_others():
    """docling-core leaves a picture with an empty ``prov`` out of every page
    export, so only the provenanced one is printed, with its own ref."""
    document = _docling_document(n_pages=1)
    _add_picture(document, None)
    _add_text(document, 1, "Body")
    _add_picture(document, 1)

    text_blocks, image_blocks = _postprocess(document)

    assert [b.page_number for b in image_blocks] == [None, 1]
    assert text_blocks[0].text == "Body\n\n![](docling_img_1)"


def test_an_unreadable_picture_prints_nothing_and_keeps_its_caption():
    """No block, so no ref, and no ``<!-- image -->`` left in its place."""
    document = _docling_document(n_pages=1)
    _add_text(document, 1, "Before")
    broken = _add_picture(document, 1)
    broken.image = ImageRef(mimetype="image/png", dpi=72, size=Size(width=2, height=2), uri="file:///missing.png")
    caption = _add_text(document, 1, "Figure 1: lost", label=DocItemLabel.CAPTION)
    broken.captions.append(caption.get_ref())
    _add_text(document, 1, "After")
    _add_picture(document, 1)

    text_blocks, image_blocks = _postprocess(document)

    assert [b.metadata["markdown_ref"] for b in image_blocks] == ["![](docling_img_1)"]
    assert text_blocks[0].text == "Before\n\nFigure 1: lost\n\nAfter\n\n![](docling_img_1)"


def test_every_page_docling_kept_is_exported_under_its_own_number():
    """Page 2 left out of the document: counting ``result.pages`` from 1
    exported a page 2 that isn't there and lost page 3 with its pictures."""
    document = DoclingDocument(name="report")
    for page_no in (1, 3):
        document.add_page(page_no=page_no, size=Size(width=100, height=100))
    _add_text(document, 1, "First page")
    _add_text(document, 3, "Last page")
    _add_picture(document, 3)

    text_blocks, _ = _postprocess(document)

    assert [(b.page_number, b.text) for b in text_blocks] == [
        (1, "First page"),
        (3, "Last page\n\n![](docling_img_0)"),
    ]


def test_the_page_text_is_docling_s_own_page_export_apart_from_the_pictures():
    """Pins ``_page_markdown`` to ``export_to_markdown``'s defaults (labels,
    layers, escaping), so a docling-core upgrade that changes them shows here."""
    document = _docling_document(n_pages=2)
    _add_text(document, 1, "Title", label=DocItemLabel.TITLE)
    _add_text(document, 1, "snake_case <b>body</b>")
    _add_text(document, 1, "Running header", label=DocItemLabel.PAGE_HEADER)
    _add_text(document, 2, "Second page")

    for page_no in (1, 2):
        expected = document.export_to_markdown(page_no=page_no).strip()
        assert docling_workers._page_markdown(document, page_no, {}) == expected


# ----------------------------------------------------------------------------------
# #1312: limited to one page, docling-core's export serializes every item of the
# document to keep the ones on that page, and rebuilds its whole-document lookups,
# so exporting each page cost pages × items: minutes for a few hundred pages.
# ----------------------------------------------------------------------------------


def _table_data() -> TableData:
    cells = [
        TableCell(
            text=f"cell {row}{col}",
            start_row_offset_idx=row,
            end_row_offset_idx=row + 1,
            start_col_offset_idx=col,
            end_col_offset_idx=col + 1,
        )
        for row in range(2)
        for col in range(2)
    ]
    return TableData(num_rows=2, num_cols=2, table_cells=cells)


def _document_a_page_export_treats_specially() -> tuple[DoclingDocument, dict[str, str]]:
    """Pages 1 to 4 and 6, no item on page 4; lists and a section that cross
    pages, captions on another page than their picture or table, a caption in
    the furniture layer, a chart, furniture, and items without provenance."""
    document = DoclingDocument(name="report")
    for page_no in (1, 2, 3, 4, 6):
        document.add_page(page_no=page_no, size=Size(width=100, height=100))

    document.add_title(text="Annual report", prov=_prov(1))
    document.add_text(label=DocItemLabel.PAGE_HEADER, text="Running header", prov=_prov(1))
    logo_caption = document.add_text(
        label=DocItemLabel.CAPTION,
        text="Logo, captioned in the furniture",
        prov=_prov(1),
        content_layer=ContentLayer.FURNITURE,
    )
    _add_picture(document, 1, caption=logo_caption)
    steps = document.add_list_group()
    document.add_list_item(text="first step", enumerated=True, prov=_prov(1), parent=steps)
    second = document.add_list_item(text="second step", enumerated=True, prov=_prov(2), parent=steps)
    nested = document.add_list_group(parent=second)
    document.add_list_item(text="nested on page 3", prov=_prov(3), parent=nested)
    split = document.add_list_item(text="", enumerated=True, prov=_prov(2), parent=steps)
    inline = document.add_inline_group(parent=split)
    document.add_text(label=DocItemLabel.TEXT, text="inline on page 2", prov=_prov(2), parent=inline)
    document.add_text(label=DocItemLabel.TEXT, text="inline on page 3", prov=_prov(3), parent=inline)

    section = document.add_group(label=GroupLabel.SECTION)
    document.add_heading(text="Results", prov=_prov(2), parent=section)
    document.add_text(label=DocItemLabel.TEXT, text="text_with <markup>", prov=_prov(2), parent=section)
    caption = document.add_text(label=DocItemLabel.CAPTION, text="Figure 1, captioned on page 3", prov=_prov(3))
    figure = _add_picture(document, 2, caption=caption, parent=section)
    document.add_text(label=DocItemLabel.TEXT, text="Section end on page 3", prov=_prov(3), parent=section)

    table_caption = document.add_text(label=DocItemLabel.CAPTION, text="Table 1, captioned on page 6", prov=_prov(6))
    document.add_table(data=_table_data(), caption=table_caption, prov=_prov(3))
    chart = _add_picture(document, 3, annotations=[PictureTabularChartData(title="chart", chart_data=_table_data())])
    _add_picture(document, 6, content_layer=ContentLayer.FURNITURE)
    document.add_text(label=DocItemLabel.TEXT, text="No provenance")
    document.add_formula(text="E = mc^2", prov=_prov(6))
    document.add_text(label=DocItemLabel.FOOTNOTE, text="Footnote on page 6", prov=_prov(6))

    refs = {figure.self_ref: "![](docling_img_0)", chart.self_ref: "![](docling_img_1)"}
    return document, refs


def _docling_core_page_export(document: DoclingDocument, page_no: int, refs: dict[str, str]) -> str:
    serializer = MarkdownDocSerializer(
        doc=document,
        picture_serializer=docling_workers._PictureRefSerializer(refs),
        params=MarkdownParams(pages={page_no}, image_placeholder=docling_workers._IMAGE_PLACEHOLDER),
    )
    return serializer.serialize().text.strip()


def test_each_page_export_is_the_one_docling_core_makes_for_that_page():
    document, refs = _document_a_page_export_treats_specially()
    index = docling_workers._PageIndex(document)

    for page_no in sorted(document.pages):
        expected = _docling_core_page_export(document, page_no, refs)
        assert docling_workers._page_markdown(document, page_no, refs, index) == expected
    # Without an index, _page_markdown builds its own.
    assert docling_workers._page_markdown(document, 3, refs) == docling_workers._page_markdown(document, 3, refs, index)


class _RandomDocument:
    """A document built at random through docling-core's API: pages with gaps,
    sections, nested lists and list items made of inline groups, pictures and
    tables captioned on any page and in either layer, charts, text inside
    pictures, furniture, items without provenance, and the item types the
    fallback serializer prints."""

    def __init__(self, rng: random.Random, n_pages: int) -> None:
        self.rng = rng
        self.document = DoclingDocument(name="random")
        self.pages = sorted(rng.sample(range(1, n_pages + 3), n_pages))
        for page_no in self.pages:
            self.document.add_page(page_no=page_no, size=Size(width=100, height=100))
        self.page_no = self.pages[0]
        self.counter = itertools.count()
        for _ in range(rng.randint(0, 40)):
            self.element(None)

    def prov(self) -> ProvenanceItem | None:
        if self.rng.random() < 0.05:
            return None
        if self.rng.random() < 0.25:  # move to the next page, now and then to any page
            if self.rng.random() < 0.2:
                self.page_no = self.rng.choice(self.pages)
            else:
                self.page_no = self.pages[min(self.pages.index(self.page_no) + 1, len(self.pages) - 1)]
        return _prov(self.page_no)

    def layer(self) -> ContentLayer | None:
        return ContentLayer.FURNITURE if self.rng.random() < 0.08 else None

    def text(self, parent: NodeItem | None = None) -> None:
        labels = [DocItemLabel.TEXT] * 6 + [DocItemLabel.FOOTNOTE, DocItemLabel.PAGE_HEADER, DocItemLabel.CAPTION]
        label = self.rng.choice(labels)
        text = f"t{next(self.counter)} a_b <i>"
        item = self.document.add_text(
            label=label, text=text, prov=self.prov(), parent=parent, content_layer=self.layer()
        )
        if item.prov and self.rng.random() < 0.1:  # text that continues on another page
            item.prov.append(_prov(self.rng.choice(self.pages)))

    def caption(self, prefix: str) -> TextItem | None:
        if self.rng.random() >= 0.6:
            return None
        return self.document.add_text(
            label=DocItemLabel.CAPTION,
            text=f"{prefix}{next(self.counter)}",
            prov=self.prov(),
            content_layer=self.layer(),
        )

    def list_group(self, parent: NodeItem | None, depth: int) -> None:
        group = self.document.add_list_group(parent=parent, content_layer=self.layer())
        enumerated = self.rng.random() < 0.5
        for _ in range(self.rng.randint(0, 5)):
            if self.rng.random() < 0.15:
                item = self.document.add_list_item(text="", enumerated=enumerated, prov=self.prov(), parent=group)
                inline = self.document.add_inline_group(parent=item)
                for _ in range(self.rng.randint(1, 3)):
                    self.text(inline)
            else:
                text = f"li{next(self.counter)}"
                item = self.document.add_list_item(text=text, enumerated=enumerated, prov=self.prov(), parent=group)
            if depth < 2 and self.rng.random() < 0.2:
                self.list_group(item, depth + 1)

    def element(self, parent: NodeItem | None, depth: int = 0) -> None:
        kind = self.rng.random()
        document = self.document
        if kind < 0.35:
            self.text(parent)
        elif kind < 0.42:
            document.add_heading(text=f"h{next(self.counter)}", prov=self.prov(), parent=parent)
        elif kind < 0.45:
            document.add_title(text=f"T{next(self.counter)}", prov=self.prov(), parent=parent)
        elif kind < 0.55:
            self.list_group(parent, 0)
        elif kind < 0.6:
            inline = document.add_inline_group(parent=parent)
            for _ in range(self.rng.randint(0, 3)):
                self.text(inline)
        elif kind < 0.68 and depth < 2:
            label = self.rng.choice([GroupLabel.SECTION, GroupLabel.CHAPTER, GroupLabel.UNSPECIFIED])
            group = document.add_group(label=label, parent=parent, content_layer=self.layer())
            for _ in range(self.rng.randint(0, 4)):
                self.element(group, depth + 1)
        elif kind < 0.8:
            caption = self.caption("cap")
            chart = self.rng.random() < 0.05
            annotations = [PictureTabularChartData(title="chart", chart_data=_table_data())] if chart else None
            picture = document.add_picture(
                caption=caption, prov=self.prov(), parent=parent, content_layer=self.layer(), annotations=annotations
            )
            if self.rng.random() < 0.3:
                self.text(picture)
        elif kind < 0.88:
            caption = self.caption("tcap")
            document.add_table(
                data=_table_data(), caption=caption, prov=self.prov(), parent=parent, content_layer=self.layer()
            )
        elif kind < 0.92:
            document.add_formula(text=f"x^{next(self.counter)}", prov=self.prov(), parent=parent)
        elif kind < 0.95:
            document.add_code(text=f"code{next(self.counter)}", prov=self.prov(), parent=parent)
        elif kind < 0.975:
            document.add_key_values(graph=GraphData(cells=[], links=[]), prov=self.prov(), parent=parent)
        else:
            document.add_form(graph=GraphData(cells=[], links=[]), prov=self.prov(), parent=parent)


def test_each_page_export_matches_docling_core_on_random_documents():
    # _PageIndex reproduces parts of docling-core (get_excluded_refs, the
    # _captions_of_some_item lookup, the traversal in get_parts). The seeds are
    # fixed, so a docling-core upgrade that changes any of them fails here.
    compared, differ = 0, []
    for seed in range(200):
        rng = random.Random(seed)
        document = _RandomDocument(rng, rng.randint(1, 6)).document
        pictures = enumerate(document.pictures)
        refs = {picture.self_ref: f"![](docling_img_{i})" for i, picture in pictures if rng.random() < 0.7}
        index = docling_workers._PageIndex(document)
        for page_no in sorted(document.pages):
            compared += 1
            exported = docling_workers._page_markdown(document, page_no, refs, index)
            if exported != _docling_core_page_export(document, page_no, refs):
                differ.append((seed, page_no))
    assert compared > 600
    assert differ == []


def test_a_page_serializer_cannot_be_built_without_an_index():
    document, _ = _document_a_page_export_treats_specially()
    with pytest.raises(TypeError, match="index"):
        docling_workers._PageMarkdownSerializer(doc=document)


def test_exporting_every_page_reads_each_item_a_bounded_number_of_times(monkeypatch):
    n_pages, per_page = 40, 5
    document = _docling_document(n_pages)
    for page_no in range(1, n_pages + 1):
        for i in range(per_page):
            _add_text(document, page_no, f"page {page_no} line {i}")

    visits = serializations = 0
    iterate_items = DoclingDocument.iterate_items
    serialize = DocSerializer.serialize

    def _counting_iterate_items(self, *args, **kwargs):
        nonlocal visits
        for entry in iterate_items(self, *args, **kwargs):
            visits += 1
            yield entry

    def _counting_serialize(self, *args, **kwargs):
        nonlocal serializations
        serializations += 1
        return serialize(self, *args, **kwargs)

    monkeypatch.setattr(DoclingDocument, "iterate_items", _counting_iterate_items)
    monkeypatch.setattr(DocSerializer, "serialize", _counting_serialize)

    blocks = docling_workers.DoclingLoader._build_text_blocks(SimpleNamespace(document=document))

    n_items = n_pages * per_page
    assert [block.page_number for block in blocks] == list(range(1, n_pages + 1))
    assert blocks[-1].text == "\n\n".join(f"page {n_pages} line {i}" for i in range(per_page))
    # Exporting each page with docling-core alone: about 4 × pages × items
    # visits and pages × items serializations.
    assert visits <= 10 * n_items
    assert serializations <= 2 * n_items


class _WidthVLM:
    """Captions an image with its width, so each caption names its picture."""

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return f"Figure {image.width} pixels wide"


def _words(text: str) -> int:
    return len(text.split())


@pytest.mark.parametrize(
    "chunker",
    [
        RecursiveSplitter(chunk_size=60, chunk_overlap_rate=0.0, length_function=_words),
        StructuredSectionChunker(chunk_size=60, length_function=_words),
    ],
    ids=["recursive", "structured_section"],
)
@pytest.mark.parametrize("captioning", [True, False], ids=["captioned", "not-captioned"])
async def test_docling_pictures_through_captioning_and_chunking(monkeypatch, chunker, captioning):
    """#1313 end to end: Docling's output, then the caption stage (or the ref
    scrub when captioning is off), then a real chunker."""

    @asynccontextmanager
    async def _no_gate():
        yield

    monkeypatch.setattr(caption_module, "get_vlm_semaphore", _no_gate)
    filler = " ".join(f"word{i}" for i in range(40))
    document = _docling_document()
    _add_text(document, 1, f"Opening remarks {filler}")
    _add_picture(document, 1, width=3)
    _add_picture(document, 1, width=4, content_layer=ContentLayer.FURNITURE)
    _add_text(document, 2, f"Middle page {filler}")
    _add_text(document, 3, f"Closing remarks {filler}")
    _add_picture(document, 3, width=5)
    text_blocks, image_blocks = _postprocess(document)
    processed = ProcessedDocument(
        document_id="d", text_blocks=text_blocks, images=image_blocks, metadata={"filename": "report.pdf"}
    )
    if captioning:
        row = await caption_stage({"processed_document": processed}, _WidthVLM())
        processed = row["processed_document"]
    processed = drop_uncaptioned_image_refs(processed)

    chunks = chunker.chunk(processed, "p")

    text = "\n".join(chunk.text for chunk in chunks)
    assert "<!-- image -->" not in text
    assert "docling_img" not in text
    assert all(chunk.page_number == 3 for chunk in chunks if "Closing remarks" in chunk.text)
    if captioning:
        for width, page in ((3, 1), (4, 1), (5, 3)):
            pages = [chunk.page_number for chunk in chunks if f"Figure {width} pixels wide" in chunk.text]
            assert pages == [page], f"caption of the {width}px picture: pages {pages}, expected [{page}]"


class _UndecodedImage:
    """An ``ImageRef`` whose ``pil_image`` must not be read: decoding the PNG
    only to encode it again is the work the loader skips."""

    def __init__(self, uri):
        self.uri = uri

    @property
    def pil_image(self):
        raise AssertionError("the picture was decoded")


def test_pictures_reuse_the_png_docling_already_encoded():
    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100
    picture = SimpleNamespace(image=_UndecodedImage(_png_data_uri(png)))

    block = docling_workers.DoclingLoader._build_image_block(picture, 0, 1)

    assert block.image_bytes == png
    assert block.pil_image is None
    assert block.metadata == {"markdown_ref": "![](docling_img_0)"}


@pytest.mark.parametrize("uri", [None, "file:///tmp/figure.png", "data:image/jpeg;base64,/9j/"])
def test_a_picture_without_a_png_data_uri_is_handed_over_unencoded(uri):
    """The caption stage encodes it, and only when the partition captions."""
    figure = object()
    picture = SimpleNamespace(image=SimpleNamespace(uri=uri, pil_image=figure))

    block = docling_workers.DoclingLoader._build_image_block(picture, 0, 1)

    assert block.pil_image is figure
    assert block.image_bytes == b""


def test_a_picture_without_an_image_has_no_block_and_logs_no_read_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(docling_workers.logger, "warning", warnings.append)

    block = docling_workers.DoclingLoader._build_image_block(SimpleNamespace(image=None), 0, 1)

    assert block is None
    assert warnings == []
