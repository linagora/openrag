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
    *, pdf="MarkerLoader", audio="LocalWhisperLoader", image_captioning=True, vlm_base_url=""
) -> SimpleNamespace:
    file_loaders = SimpleNamespace(
        pdf=pdf,
        mp3=audio,
        wav=audio,
        flac=audio,
        mp4=audio,
    )
    loader = SimpleNamespace(file_loaders=file_loaders, image_captioning=image_captioning)
    vlm = SimpleNamespace(base_url=vlm_base_url, model="m", api_key="k", timeout=60)
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


def test_build_caption_vlm_requires_endpoint() -> None:
    # No VLM endpoint configured -> unavailable, regardless of the captioning flag.
    assert build_caption_vlm(_config(image_captioning=True, vlm_base_url="")) is None
    assert build_caption_vlm(_config(image_captioning=False, vlm_base_url="")) is None


def test_build_caption_vlm_available_when_endpoint_set_even_if_globally_off() -> None:
    # Availability is decoupled from the captioning policy: an endpoint is enough
    # to build the VLM. Standalone-image captioning relies on this (the policy
    # gate lives in the pipeline, not here).
    assert build_caption_vlm(_config(image_captioning=False, vlm_base_url="http://vlm:8000/v1")) is not None
