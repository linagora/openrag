"""Unit tests for the content-type → parser dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.parsers.parser_dispatcher import (
    ParserDispatcher,
    build_caption_vlm,
)


def _config(
    *,
    pdf="MarkerLoader",
    audio="LocalWhisperLoader",
    image_captioning=True,
    vlm_base_url="",
    vlm_enable_thinking=None,
    openai_loader_enable_thinking=None,
) -> SimpleNamespace:
    file_loaders = SimpleNamespace(
        pdf=pdf,
        mp3=audio,
        wav=audio,
        flac=audio,
        mp4=audio,
    )
    openai = SimpleNamespace(
        base_url="http://openai:8000/v1",
        model="dotsocr-model",
        api_key="k",
        timeout=60,
        concurrency_limit=20,
        enable_thinking=openai_loader_enable_thinking,
    )
    transcriber = SimpleNamespace(
        base_url="http://transcriber:8000/v1",
        api_key="k",
        model_name="asr-model",
        timeout=60,
        direct_upload_suffixes={".mp3", ".wav"},
        use_whisper_lang_detector=False,
        max_concurrent_chunks=1,
    )
    loader = SimpleNamespace(
        file_loaders=file_loaders,
        image_captioning=image_captioning,
        openai=openai,
        transcriber=transcriber,
    )
    vlm = SimpleNamespace(
        base_url=vlm_base_url,
        model="m",
        api_key="k",
        timeout=60,
        enable_thinking=vlm_enable_thinking,
    )
    return SimpleNamespace(loader=loader, vlm=vlm)


class _FakeParser:
    def __init__(self) -> None:
        self.seen: Document | None = None

    def supported_types(self) -> list[str]:
        return []

    async def parse(self, document: Document) -> ProcessedDocument:
        self.seen = document
        return ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="ok")])


@pytest.mark.parametrize(
    ("filename", "content_type", "expected_backend"),
    [
        ("a.txt", DocumentType.TEXT, "text"),
        ("a.md", DocumentType.MARKDOWN, "markdown"),
        ("a.html", DocumentType.HTML, "html"),
        ("a.docx", DocumentType.DOCX, "docx"),
        ("a.doc", DocumentType.DOC, "doc"),
        ("a.pptx", DocumentType.PPTX, "pptx"),
        ("a.eml", DocumentType.EML, "eml"),
        ("a.png", DocumentType.IMAGE, "image"),
        ("a.svg", DocumentType.IMAGE, "image"),
        ("a.gif", DocumentType.IMAGE, "image"),
        ("a.webp", DocumentType.IMAGE, "image"),
        ("a.bmp", DocumentType.IMAGE, "image"),
        ("a.pdf", DocumentType.PDF, "marker"),
        ("a.mp3", DocumentType.AUDIO, "local_whisper"),
        ("a.mp4", DocumentType.VIDEO, "local_whisper"),
    ],
)
def test_resolve_backend(filename: str, content_type: DocumentType, expected_backend: str) -> None:
    disp = ParserDispatcher(_config())
    from services.workers.parsers.parser_dispatcher import _suffix

    assert disp._resolve_backend(content_type, _suffix(filename)) == expected_backend


def test_resolve_pdf_backend_variants() -> None:
    assert ParserDispatcher(_config(pdf="DoclingLoader"))._resolve_pdf_backend() == "docling"
    assert ParserDispatcher(_config(pdf="PyMuPDFLoader"))._resolve_pdf_backend() == "pymupdf"
    assert ParserDispatcher(_config(pdf="DotsOCRLoader"))._resolve_pdf_backend() == "pdf_client"


def test_resolve_audio_backend_openai() -> None:
    disp = ParserDispatcher(_config(audio="OpenAIAudioLoader"))
    assert disp._resolve_audio_backend("mp3") == "audio_client"


def test_audio_client_receives_live_transcription_prompt_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.parsers.parser_dispatcher as dispatcher

    async def resolver() -> str | None:
        return "Keep speaker labels."

    monkeypatch.setattr(dispatcher, "_create", lambda _module, _name, **kwargs: kwargs["client"])

    client = ParserDispatcher(
        _config(audio="OpenAIAudioLoader"),
        transcription_prompt_resolver=resolver,
    )._build_audio_client()

    assert client._transcription_prompt_resolver is resolver


def test_unsupported_pdf_config_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported PDF loader"):
        ParserDispatcher(_config(pdf="NopeLoader"))._resolve_pdf_backend()


@pytest.mark.asyncio
async def test_parse_dispatches_to_cached_backend() -> None:
    disp = ParserDispatcher(_config())
    fake = _FakeParser()
    disp._by_name["marker"] = fake  # pre-seed so no real backend is built

    document = Document(filename="report.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.4")
    result = await disp.parse(document)

    assert fake.seen is document
    assert result.text_blocks[0].text == "ok"


@pytest.mark.asyncio
async def test_for_pdf_strategy_overrides_pdf_backend_and_shares_cache() -> None:
    """A preset's ``parsing_strategy`` must override the global PDF backend for
    PDFs while non-PDF types still dispatch normally — reusing the dispatcher's
    cached backends (no duplicate pools)."""
    disp = ParserDispatcher(_config(pdf="MarkerLoader"))  # global default = marker
    marker, pymupdf, text = _FakeParser(), _FakeParser(), _FakeParser()
    disp._by_name.update({"marker": marker, "pymupdf": pymupdf, "text": text})

    pdf_parser = disp.for_pdf_strategy("pymupdf")

    pdf = Document(filename="a.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.4")
    await pdf_parser.parse(pdf)
    assert pymupdf.seen is pdf  # routed to the preset strategy, not the global marker
    assert marker.seen is None

    txt = Document(filename="a.txt", content_type=DocumentType.TEXT, raw_bytes=b"hi")
    await pdf_parser.parse(txt)
    assert text.seen is txt  # non-PDF content still dispatches by content type


def test_for_pdf_strategy_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="Unsupported PDF parsing strategy"):
        ParserDispatcher(_config()).for_pdf_strategy("nope")


def test_pymupdf_backend_builds_in_markdown_mode_without_images() -> None:
    """The lightweight pymupdf backend builds in markdown mode — structured text
    for the markdown-aware chunker — but with embed_images=False so it never
    inlines base64 images into chunk text (which bloats chunks / breaks Milvus
    inserts). Images are marker/docling's job."""
    parser = ParserDispatcher(_config(pdf="PyMuPDFLoader"))._get("pymupdf")
    assert getattr(parser, "_mode", None) == "markdown"

    # The "without images" contract: build a PDF that actually contains an image
    # and confirm the markdown extractor produces NO ImageBlocks and inlines no
    # base64 data URIs. Catches a regression that re-enables embed_images.
    import io

    import pymupdf
    from core.indexing.parsers.pdf.pymupdf import _extract_markdown
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello world.")
    page.insert_image(pymupdf.Rect(0, 0, 8, 8), stream=buf.getvalue())
    raw = doc.tobytes()
    doc.close()

    pages, images = _extract_markdown(raw, "doc.pdf")
    assert images == []  # pymupdf must not extract/inline images
    assert not any("data:image" in p for p in pages)


def test_build_caption_vlm_requires_endpoint() -> None:
    # No VLM endpoint configured -> unavailable, regardless of the captioning flag.
    assert build_caption_vlm(_config(image_captioning=True, vlm_base_url="")) is None
    assert build_caption_vlm(_config(image_captioning=False, vlm_base_url="")) is None


def test_build_caption_vlm_available_when_endpoint_set_even_if_globally_off() -> None:
    # Availability is decoupled from the captioning policy: an endpoint is enough
    # to build the VLM. Standalone-image captioning relies on this (the policy
    # gate lives in the pipeline, not here).
    assert build_caption_vlm(_config(image_captioning=False, vlm_base_url="http://vlm:8000/v1")) is not None


def test_build_caption_vlm_preserves_enable_thinking() -> None:
    vlm = build_caption_vlm(
        _config(
            image_captioning=False,
            vlm_base_url="http://vlm:8000/v1",
            vlm_enable_thinking=False,
        )
    )

    assert vlm._enable_thinking is False


def test_build_pdf_client_preserves_openai_loader_enable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePdfClient:
        def __init__(self, vlm, concurrency_limit):
            self.vlm = vlm
            self.concurrency_limit = concurrency_limit

    import services.inference.parsers.dotsocr as dotsocr
    import services.workers.parsers.parser_dispatcher as dispatcher

    monkeypatch.setattr(dotsocr, "DotsOCRPdfClient", FakePdfClient)
    monkeypatch.setattr(dispatcher, "_create", lambda _module, _name, **kwargs: kwargs["client"])

    parser = ParserDispatcher(_config(pdf="DotsOCRLoader", openai_loader_enable_thinking=False))._build_pdf_client()

    assert parser.vlm._enable_thinking is False


def test_build_eml_wires_nested_email_parser_with_depth_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    disp = ParserDispatcher(_config())
    fallback_parser = _FakeParser()
    monkeypatch.setattr(disp, "_get", lambda name: fallback_parser)

    parser = disp._build_eml()

    assert parser._attachment_parsers["txt"] is fallback_parser
    assert "eml" in parser._attachment_parsers
    nested_1 = parser._attachment_parsers["eml"]
    nested_2 = nested_1._attachment_parsers["eml"]
    nested_3 = nested_2._attachment_parsers["eml"]
    assert "eml" not in nested_3._attachment_parsers
