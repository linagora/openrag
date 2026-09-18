"""Tests for the upload content checks in ``core.indexing.validators``.

The extension a caller supplies is what selects the parser, so a file renamed
to ``.pdf`` reaches the PDF backend whatever it contains. These pin all three
parts of the rule: the formats whose signature is checked from the head, the
OOXML formats settled by reading the package, and the ones deliberately left
alone because checking them would reject legitimate uploads.
"""

from __future__ import annotations

import io
import os
import zipfile

import filetype
import pytest
from core.indexing.validators import (
    CONTENT_SNIFF_BYTES,
    validate_content_matches_extension,
    validate_ooxml_package,
)
from core.utils.exceptions import ValidationError

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64
TEXT = b"just some words, no signature at all\n"


def _zip(*names: str, payload: bytes | str = "<x/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name in names:
            archive.writestr(name, payload)
    return buf.getvalue()


def _ooxml(main_part: str, *extra: str) -> bytes:
    """A structurally complete package: both mandatory OPC parts, plus the main
    part that gives it its kind."""
    return _zip("[Content_Types].xml", "_rels/.rels", *extra, main_part)


DOCX = _ooxml("word/document.xml")
PPTX = _ooxml("ppt/presentation.xml")


@pytest.mark.parametrize(
    ("extension", "head"),
    [("pdf", PDF), ("png", PNG), ("jpg", JPG), ("jpeg", JPG), ("gif", GIF)],
)
def test_matching_content_is_accepted(extension, head):
    validate_content_matches_extension(extension, head)


@pytest.mark.parametrize("extension", ["docx", "pptx"])
def test_the_head_check_does_not_judge_ooxml(extension):
    """A ZIP is settled by its central directory, at the end of the file, so the
    head check must stay silent on docx/pptx rather than guess either way."""
    validate_content_matches_extension(extension, PDF)
    validate_content_matches_extension(extension, DOCX)


@pytest.mark.parametrize(
    ("extension", "head", "reason"),
    [
        ("pdf", PNG, "a real image renamed .pdf"),
        ("pdf", ELF, "an executable renamed .pdf"),
        ("pdf", TEXT, "unrecognised bytes renamed .pdf"),
        ("png", PDF, "a PDF renamed .png"),
    ],
)
def test_contradicting_content_is_refused(extension, head, reason):
    with pytest.raises(ValidationError) as exc_info:
        validate_content_matches_extension(extension, head)
    assert exc_info.value.status_code == 415, reason


def test_unrecognised_content_is_refused_not_waved_through():
    """The important half: arbitrary bytes sniff as nothing at all, so a rule
    that only caught *contradictions* would let them reach the parser."""
    with pytest.raises(ValidationError):
        validate_content_matches_extension("pdf", ELF)


@pytest.mark.parametrize("extension", ["txt", "md", "html", "htm", "eml", "svg", "doc", "wma", "mp3", ""])
def test_unverifiable_extensions_pass_through(extension):
    """Text formats have no signature, and .doc/.wma are not reliably detected
    by the bundled matchers. Enforcing them would refuse valid uploads."""
    validate_content_matches_extension(extension, ELF)
    validate_content_matches_extension(extension, TEXT)


def test_signature_formats_are_settled_by_the_head_alone():
    """The head check must not depend on trailing bytes: it is handed a slice."""
    validate_content_matches_extension("pdf", (PDF + os.urandom(300_000))[:CONTENT_SNIFF_BYTES])


def test_error_names_the_extension_and_what_was_found():
    with pytest.raises(ValidationError) as exc_info:
        validate_content_matches_extension("pdf", PNG)
    message = str(exc_info.value)
    assert ".pdf" in message and "png" in message


def test_real_pdf_fixture_is_accepted():
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[3] / "resources" / "test_file.pdf"
    validate_content_matches_extension("pdf", fixture.read_bytes()[:CONTENT_SNIFF_BYTES])


# ---------------------------------------------------------------------------
# OOXML packages
#
# `filetype` cannot settle docx/pptx: it looks for an entry *named* `word/` or
# `ppt/` among the first few local file headers. Both failure modes below were
# reproduced against the bundled matcher, and are asserted here so that an
# upgrade that changes them is visible rather than silent.
# ---------------------------------------------------------------------------


def _real_docx() -> bytes:
    """A document as a real producer writes it, not as this file does."""
    docx = pytest.importorskip("docx", reason="python-docx is only available transitively")
    buf = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("hello")
    document.save(buf)
    return buf.getvalue()


def _real_pptx() -> bytes:
    pptx = pytest.importorskip("pptx", reason="python-pptx is only available transitively")
    buf = io.BytesIO()
    pptx.Presentation().save(buf)
    return buf.getvalue()


def test_documents_from_a_real_producer_are_accepted():
    validate_ooxml_package("docx", io.BytesIO(_real_docx()))
    validate_ooxml_package("pptx", io.BytesIO(_real_pptx()))


def test_a_package_that_writes_the_content_type_map_last_is_accepted():
    """LibreOffice leads with ``_rels/.rels`` and writes ``[Content_Types].xml``
    last. Nothing here depends on entry order."""
    archive = _zip(
        "_rels/.rels",
        "docProps/core.xml",
        "docProps/app.xml",
        "word/_rels/document.xml.rels",
        "word/document.xml",
        "[Content_Types].xml",
    )
    validate_ooxml_package("docx", io.BytesIO(archive))


def test_a_document_whose_main_part_is_deep_in_the_archive_is_accepted():
    """The false reject. ``customXml`` parts routinely push ``word/document.xml``
    past the handful of headers the signature matcher looks at, and it then
    reports a plain zip — which, enforced, would refuse a genuine document."""
    archive = _ooxml("word/document.xml", *(f"customXml/item{i}.xml" for i in range(6)))
    assert filetype.guess(archive[:CONTENT_SNIFF_BYTES]).extension == "zip"

    validate_ooxml_package("docx", io.BytesIO(archive))


def test_a_zip_named_like_a_document_is_rejected():
    """The false accept. Any archive whose first entry starts ``word/`` is
    reported as a docx, with no content-type map and no document in it."""
    archive = _zip("word/not-a-document.txt", "payload.bin")
    assert filetype.guess(archive[:CONTENT_SNIFF_BYTES]).extension == "docx"

    with pytest.raises(ValidationError) as exc_info:
        validate_ooxml_package("docx", io.BytesIO(archive))
    assert exc_info.value.status_code == 415


@pytest.mark.parametrize(
    ("extension", "payload", "reason"),
    [
        ("docx", PDF, "a PDF renamed .docx"),
        ("pptx", DOCX, "a docx renamed .pptx"),
        ("docx", PPTX, "a pptx renamed .docx"),
    ],
)
def test_contradicting_content_is_refused_by_the_package_check(extension, payload, reason):
    """The rejections the head check used to own. Behaviour is preserved; only
    the function enforcing it changed."""
    with pytest.raises(ValidationError) as exc_info:
        validate_ooxml_package(extension, io.BytesIO(payload))
    assert exc_info.value.status_code == 415, reason


def test_the_package_relationships_part_is_required():
    """Both OPC parts are mandatory, so an archive that borrowed only a
    document's entry names is not a package."""
    with pytest.raises(ValidationError):
        validate_ooxml_package("docx", io.BytesIO(_zip("[Content_Types].xml", "word/document.xml")))


def test_a_truncated_document_is_rejected():
    """A ZIP's directory is at its end, so a half-written document cannot be
    read as a package. Every call site holds the complete file by then."""
    with pytest.raises(ValidationError):
        validate_ooxml_package("docx", io.BytesIO(_real_docx()[:2048]))


def test_a_compression_bomb_is_never_expanded():
    """Only the directory is parsed; no member is decompressed. A package whose
    parts expand to far more than memory is read without expanding them."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "\0" * (64 << 20))
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", "<w:document/>")
    validate_ooxml_package("docx", io.BytesIO(buf.getvalue()))


@pytest.mark.parametrize("extension", ["pdf", "png", "txt", "doc", ""])
def test_non_ooxml_extensions_are_left_alone(extension):
    stream = io.BytesIO(b"anything at all")
    stream.seek(4)
    validate_ooxml_package(extension, stream)
    assert stream.tell() == 4


def test_the_stream_position_is_restored_on_success_and_on_rejection():
    """Callers stream the same handle to disk afterwards. Consuming it here
    would silently truncate every accepted upload."""
    good = io.BytesIO(DOCX)
    validate_ooxml_package("docx", good)
    assert good.tell() == 0
    assert good.read() == DOCX

    bad = io.BytesIO(_zip("word/x.txt"))
    with pytest.raises(ValidationError):
        validate_ooxml_package("docx", bad)
    assert bad.tell() == 0


# ---------------------------------------------------------------------------
# Paths that reach a parser without crossing the upload routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eml_attachment_with_contradicting_content_is_skipped():
    """An attachment's filename picks its parser exactly as an upload's does,
    but those bytes never crossed the upload check. A mismatched attachment
    must be dropped, and the rest of the message must still parse."""
    from core.indexing.parsers.eml_parser import EmlParser

    called = False

    class _ExplodingParser:
        async def parse(self, document):  # pragma: no cover - must not run
            nonlocal called
            called = True
            raise AssertionError("parser reached with unverified bytes")

    parser = EmlParser(attachment_parsers={"pdf": _ExplodingParser()})
    inline, images = await parser._render_one(
        {"filename": "invoice.pdf", "raw": _PNG_BYTES, "content_type": "application/pdf", "size": len(_PNG_BYTES)},
        "pdf",
    )

    assert called is False
    assert inline == ""


@pytest.mark.asyncio
async def test_eml_attachment_with_matching_content_still_parses():
    from core.indexing.parsers.eml_parser import EmlParser
    from core.models.document import ProcessedDocument, TextBlock

    class _Parser:
        async def parse(self, document):
            return ProcessedDocument(document_id="a", text_blocks=[TextBlock(text="hello")], images=[])

    parser = EmlParser(attachment_parsers={"pdf": _Parser()})
    inline, _ = await parser._render_one(
        {"filename": "real.pdf", "raw": PDF, "content_type": "application/pdf", "size": len(PDF)},
        "pdf",
    )
    assert "hello" in inline


@pytest.mark.asyncio
async def test_eml_attachment_with_no_registered_parser_is_still_checked():
    """An attachment with no parser falls through to the image path and is
    emitted for captioning. Validating inside the parser branch would leave
    that route unchecked, so the check runs before the branch."""
    from core.indexing.parsers.eml_parser import EmlParser

    parser = EmlParser(attachment_parsers={})  # nothing registered for png
    inline, images = await parser._render_one(
        {"filename": "photo.png", "raw": PDF, "content_type": "image/png", "size": len(PDF)},
        "png",
    )

    assert inline == ""
    assert images == []


@pytest.mark.asyncio
async def test_genuine_image_attachment_without_a_parser_still_becomes_an_image():
    from core.indexing.parsers.eml_parser import EmlParser

    parser = EmlParser(attachment_parsers={})
    _, images = await parser._render_one(
        {"filename": "photo.png", "raw": _PNG_BYTES, "content_type": "image/png", "size": len(_PNG_BYTES)},
        "png",
    )

    assert len(images) == 1
