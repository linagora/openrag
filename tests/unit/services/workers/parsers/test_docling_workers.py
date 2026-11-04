from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from core.models.document import Document, DocumentType
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


def _fake_conversion(n_pages: int, n_pictures: int, export=lambda page_no: "text"):
    """The slice of a ``ConversionResult`` that the block builders read."""
    picture = SimpleNamespace(image=SimpleNamespace(pil_image=object()))
    return SimpleNamespace(
        pages=[None] * n_pages,
        document=SimpleNamespace(export_to_markdown=export, pictures=[picture] * n_pictures),
    )


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

    def _slow_export(page_no):
        window.append(ticks)
        time.sleep(0.5)
        window.append(ticks)
        return "text"

    async def _fake_dispatch(path):
        return _fake_conversion(n_pages=1, n_pictures=0, export=_slow_export)

    monkeypatch.setattr(loader, "_dispatch", _fake_dispatch)

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


@pytest.mark.parametrize("slow_step", ["page export", "picture encoding"])
async def test_cancelling_parse_stops_building_the_blocks(monkeypatch, slow_step):
    """A cancelled parse must not leave the worker thread exporting every
    remaining page or encoding every remaining picture."""
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
        result = _fake_conversion(n_pages=total, n_pictures=0, export=_slow("text"))
    else:
        result = _fake_conversion(n_pages=1, n_pictures=total)
        monkeypatch.setattr(docling_workers, "pil_to_png_bytes", _slow(b"png"))

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
# wherever it occurs in the page text. A ref the exporter never printed meant every
# caption was appended at the end of the document and attributed to the last page,
# with the exporter's own ``<!-- image -->`` markers left in the indexed text.
# ----------------------------------------------------------------------------------


def _prov(page_no: int | None) -> list[SimpleNamespace]:
    """The slice of docling-core's ``ProvenanceItem`` the loader reads."""
    return [] if page_no is None else [SimpleNamespace(page_no=page_no)]


def _fake_document(pages_text: dict[int, str], pictures: list[int | None]) -> SimpleNamespace:
    """A conversion-result slice: per-page exports + pictures with provenance.

    ``pictures`` holds one provenance page number (``None`` = empty provenance)
    per picture, in document traversal order — the order the exporter prints
    ``<!-- image -->`` placeholders in (pinned by the docling-core relationship
    test below). Each picture renders a real 2×2 PNG, so the production
    encoding path runs for real in these tests.
    """

    def _picture(page_no: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            image=SimpleNamespace(pil_image=Image.new("RGB", (2, 2))),
            prov=_prov(page_no),
        )

    return SimpleNamespace(
        pages=[None] * max(pages_text, default=0),
        document=SimpleNamespace(
            export_to_markdown=lambda page_no: pages_text.get(page_no, ""),
            pictures=[_picture(p) for p in pictures],
        ),
    )


def _loader() -> docling_workers.DoclingLoader:
    """A ``DoclingLoader`` without the config/Ray wiring ``_postprocess`` skips."""
    return docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)


def test_placeholders_are_swapped_by_position_with_the_pictures_of_the_same_page():
    """Two pictures on one page: each occurrence gets its own ref, in order."""
    result = _fake_document(
        {1: "Before\n\n<!-- image -->\n\nMiddle\n\n<!-- image -->\n\nAfter"},
        pictures=[1, 1],
    )

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert [b.text for b in text_blocks] == ["Before\n\n![](docling_img_0)\n\nMiddle\n\n![](docling_img_1)\n\nAfter"]
    assert [b.metadata["markdown_ref"] for b in image_blocks] == ["![](docling_img_0)", "![](docling_img_1)"]
    assert [b.page_number for b in image_blocks] == [1, 1]


def test_each_page_maps_only_its_own_pictures():
    """Images on pages 1 and 3 with no picture on page 2: no cross-page bleed."""
    result = _fake_document({1: "first\n\n<!-- image -->", 2: "text only", 3: "<!-- image -->"}, pictures=[1, 3])

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert [b.page_number for b in image_blocks] == [1, 3]
    assert [b.text for b in text_blocks] == ["first\n\n![](docling_img_0)", "text only", "![](docling_img_1)"]


def test_failed_encoding_keeps_its_position_and_does_not_shift_later_refs(monkeypatch):
    """Picture 2 fails to encode: its occurrence stays an inert ``<!-- image -->``
    (no ref pointing at a nonexistent block) and picture 3 still lands at its
    own occurrence instead of sliding into picture 2's slot."""
    result = _fake_document(
        {1: "a\n\n<!-- image -->\n\nb\n\n<!-- image -->\n\nc\n\n<!-- image -->\n\nd"},
        pictures=[1, 1, 1],
    )
    calls = {"n": 0}

    def _flaky(_pil_image):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("encode blew up")
        return b"png"

    monkeypatch.setattr(docling_workers, "pil_to_png_bytes", _flaky)

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert text_blocks[0].text == ("a\n\n![](docling_img_0)\n\nb\n\n<!-- image -->\n\nc\n\n![](docling_img_2)\n\nd")
    # numbering stays stable: the failed picture consumed its index
    assert [b.metadata["markdown_ref"] for b in image_blocks] == ["![](docling_img_0)", "![](docling_img_2)"]
    assert [b.page_number for b in image_blocks] == [1, 1]


def test_picture_without_provenance_gets_none_page_and_leaves_its_placeholder():
    """Empty prov: the block carries ``page_number=None`` and, owning no slot,
    its ref never enters the page text — the caption stage later falls back to
    appending with a ``None`` page, which is what happened before, while every
    provenanced picture on other pages still anchors correctly."""
    result = _fake_document(
        {1: "one\n\n<!-- image -->", 2: "two\n\n<!-- image -->"},
        pictures=[None, 2],
    )

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert image_blocks[0].page_number is None
    assert image_blocks[1].page_number == 2
    assert "![](docling_img_0)" not in text_blocks[0].text
    assert text_blocks[0].text == "one\n\n<!-- image -->"
    assert text_blocks[1].text == "two\n\n![](docling_img_1)"


def test_unprovenanced_picture_blocks_the_alignment_of_its_own_page():
    """A provenanced picture next to an unprovenanced one: which occurrence the
    provenanced picture owns cannot be known, so exact alignment fails and the
    whole page is left untouched instead of risking a wrong assignment."""
    result = _fake_document({1: "one\n\n<!-- image -->\n\n<!-- image -->"}, pictures=[None, 1])

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert [b.page_number for b in image_blocks] == [None, 1]
    assert text_blocks[0].text == "one\n\n<!-- image -->\n\n<!-- image -->"


def test_placeholder_count_mismatch_leaves_the_page_untouched():
    """More occurrences on the page than pictures mapped for it: which
    occurrence a real ref would own cannot be known, so the conservative
    alignment guard swaps nothing instead of risking a wrong assignment."""
    result = _fake_document({1: "x\n\n<!-- image -->\n\ny\n\n<!-- image -->"}, pictures=[1])

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert len(image_blocks) == 1
    assert text_blocks[0].text == "x\n\n<!-- image -->\n\ny\n\n<!-- image -->"


def test_slot_surplus_leaves_the_page_untouched_rather_than_gambling():
    """More pictures than occurrences (e.g. a chart exported as a table): the
    page cannot tell which picture owns the printed placeholder, so nothing is
    swapped; both captions later fall back to appending at their own page."""
    result = _fake_document({1: "single\n\n<!-- image -->"}, pictures=[1, 1])

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert [b.metadata["markdown_ref"] for b in image_blocks] == ["![](docling_img_0)", "![](docling_img_1)"]
    assert text_blocks[0].text == "single\n\n<!-- image -->"
    assert text_blocks[0].text.count("docling_img") == 0


def test_swap_helper_maps_slots_in_order_without_reshuffling():
    """Direct guard on the positional helper for the alignment shape the loader
    guarantees: one slot per occurrence, ``None`` keeping its occurrence."""
    text = "a<!-- image -->b<!-- image -->c"

    assert docling_workers._swap_image_placeholders(text, []) == text
    assert docling_workers._swap_image_placeholders(text, [None, "ref"]) == "a<!-- image -->brefc"
    assert docling_workers._swap_image_placeholders(text, ["ref0", None]) == "aref0b<!-- image -->c"


def test_docling_export_order_matches_the_pictures_collection():
    """Pin the positional assumption on real docling-core objects: the exporter
    prints one ``<!-- image -->`` per picture, and the k-th occurrence of a
    page is the k-th picture of that page in ``document.pictures`` order."""
    from docling_core.types.doc.base import BoundingBox
    from docling_core.types.doc.document import DoclingDocument, ProvenanceItem

    doc = DoclingDocument(name="pages-order")
    for page_no in (1, 1, 3):
        doc.add_picture(prov=ProvenanceItem(page_no=page_no, bbox=BoundingBox(l=0, t=0, r=10, b=10), charspan=(0, 4)))
    # stub render blobs the same way real picture items carry theirs
    for picture in doc.pictures:
        picture.image = SimpleNamespace(pil_image=Image.new("RGB", (2, 2)))
    result = SimpleNamespace(pages=[None] * 3, document=doc)

    for page_no, expected in ((1, 2), (2, 0), (3, 1)):
        assert doc.export_to_markdown(page_no=page_no).count(docling_workers._IMAGE_PLACEHOLDER) == expected

    text_blocks, image_blocks = _loader()._postprocess(result, threading.Event())

    assert [b.page_number for b in image_blocks] == [1, 1, 3]
    assert [b.text for b in text_blocks] == [
        "![](docling_img_0)\n\n![](docling_img_1)",
        "",
        "![](docling_img_2)",
    ]
