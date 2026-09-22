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
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64  # compound file: .doc, and .xls/.ppt/.msi too
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


@pytest.mark.parametrize("extension", ["txt", "md", "html", "htm", "eml", "svg", "wma", "mp3", ""])
def test_unverifiable_extensions_pass_through(extension):
    """Text formats have no signature, and .wma is not reliably detected by the
    bundled matchers. Enforcing them would refuse valid uploads.

    ``.doc`` used to be on this list — it is checked by its container signature
    now (#964), so it has its own tests below."""
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


# ---------------------------------------------------------------------------
# .doc — the one accepted format that had no check at all (#964, audit A2)
# ---------------------------------------------------------------------------
#
# Spire.Doc loads OLE2, RTF, HTML and plain text under a .doc name — verified
# against the real library — and Word has written all four that way. So .doc
# cannot use the allowlist the other formats use: requiring a known-good
# signature would refuse uploads that index today, which is why .doc was left
# unchecked in #957. It refuses what `filetype` recognises as something else
# instead.

RTF = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Times;}}\f0\fs24 hello\par}"
HTML_DOC = b"<html><body><p>a .doc that is really html</p></body></html>"


def _word_doc(marker: str) -> bytes:
    """A Word 97-2003 document in one of the two shapes ``filetype`` recognises.

    The short OLE2 stub above is *not* enough: ``filetype.guess`` returns None
    for it, so a test built only on that never reaches the branch a real
    document takes — which is how a 415 on genuine .doc files got this far.
    """
    buf = bytearray(b"\x00" * 4096)
    buf[0:8] = OLE2[:8]
    if marker == "fib":
        buf[512:516] = b"\xec\xa5\xc1\x00"
    else:
        word8 = b"\x00\x0a\x00\x00\x00MSWordDoc\x00\x10\x00\x00\x00Word.Document.8\x00\xf49\xb2q"
        buf[2075 : 2075 + len(word8)] = word8
    return bytes(buf)


@pytest.mark.parametrize(
    ("head", "why"),
    [
        (OLE2, "a compound-file document filetype cannot place"),
        (_word_doc("fib"), "a real Word 97-2003 doc — filetype reports 'doc'"),
        (_word_doc("word8"), "the Word.Document.8 shape, likewise"),
        (RTF, "RTF, which Word wrote under .doc for years"),
        (HTML_DOC, "HTML, likewise"),
        (TEXT, "plain text, which has no signature by definition"),
        (b"", "an empty upload, which Spire rejects on its own"),
    ],
)
def test_everything_spire_can_load_is_still_accepted(head, why):
    """Guard against re-introducing the gap: a stricter rule here refuses
    uploads that index today, which is what kept .doc unchecked until now."""
    validate_content_matches_extension("doc", head)
    assert why


@pytest.mark.parametrize(
    ("head", "detected"),
    [
        (PDF, "pdf"),
        # ``filetype`` reports *docx* here, not zip: its matcher keys on an entry
        # named ``word/`` near the head — the same quirk ``validate_ooxml_package``
        # exists for. Either way it is a recognised foreign format and is refused.
        (_zip("word/document.xml"), "docx"),
        (_zip("payload.bin"), "zip"),
        (ELF, "elf"),
        (PNG, "png"),
        (b"\x1f\x8b\x08" + b"\x00" * 64, "gz"),
    ],
)
def test_a_recognised_foreign_format_no_longer_reaches_the_doc_parser(head, detected):
    """The gap this closes. Every parser-bomb vector worth the name is a
    structured format, and ``filetype`` names each one."""
    with pytest.raises(ValidationError, match=detected):
        validate_content_matches_extension("doc", head)


def test_unsignatured_content_still_reaches_the_parser_and_that_is_deliberate():
    """Records the trade rather than leaving it implicit: .doc is a blocklist
    where every other format is an allowlist. Arbitrary bytes with no signature
    reach Spire, fail its load, and fall back to ``GetText()``. Narrowing this
    to OLE2+RTF is possible once the corpora are known to hold no HTML/text
    .doc files — a corpus question, not a code one."""
    validate_content_matches_extension("doc", b"\x01\x02\x03 arbitrary, unrecognised")


def test_the_doc_rule_does_not_leak_to_other_extensions():
    """Only .doc is tolerant; .pdf must still require its own signature."""
    with pytest.raises(ValidationError):
        validate_content_matches_extension("pdf", TEXT)


def test_filetype_recognising_a_real_word_document_is_not_a_rejection():
    """Regression guard. ``filetype`` reports ``doc`` for a genuine Word 97-2003
    file, and an earlier version of this rule tolerated only ``rtf`` — so the
    one format the extension exists for got a 415. Caught in review on #1006."""
    real = _word_doc("fib")
    assert filetype.guess(real).extension == "doc", "guard: the fixture must be recognisable"
    validate_content_matches_extension("doc", real)
