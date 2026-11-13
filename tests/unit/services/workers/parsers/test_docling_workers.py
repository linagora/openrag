from __future__ import annotations

import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import pytest
from core.models.document import Document, DocumentType
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import ContentLayer, DoclingDocument, ImageRef, PictureItem, ProvenanceItem
from docling_core.types.doc.labels import DocItemLabel
from PIL import Image
from services.workers.parsers import docling_workers


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

    assert "Body\n\n![](docling_img_1)" in text_blocks[0].text


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
