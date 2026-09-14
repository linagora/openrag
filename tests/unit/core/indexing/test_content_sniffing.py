"""Tests for the upload content-signature check in ``core.indexing.validators``.

The extension a caller supplies is what selects the parser, so a file renamed
to ``.pdf`` reaches the PDF backend whatever it contains. These pin both halves
of the rule: the formats whose signature is checked, and the ones deliberately
left alone because checking them would reject legitimate uploads.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from core.indexing.validators import (
    CONTENT_SNIFF_BYTES,
    validate_content_matches_extension,
)
from core.utils.exceptions import ValidationError

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64
TEXT = b"just some words, no signature at all\n"


def _ooxml(entry: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(entry, "<x/>")
    return buf.getvalue()


DOCX = _ooxml("word/document.xml")
PPTX = _ooxml("ppt/presentation.xml")


@pytest.mark.parametrize(
    ("extension", "head"),
    [("pdf", PDF), ("png", PNG), ("jpg", JPG), ("jpeg", JPG), ("gif", GIF), ("docx", DOCX), ("pptx", PPTX)],
)
def test_matching_content_is_accepted(extension, head):
    validate_content_matches_extension(extension, head)


@pytest.mark.parametrize(
    ("extension", "head", "reason"),
    [
        ("pdf", PNG, "a real image renamed .pdf"),
        ("pdf", ELF, "an executable renamed .pdf"),
        ("pdf", TEXT, "unrecognised bytes renamed .pdf"),
        ("docx", PDF, "a PDF renamed .docx"),
        ("png", PDF, "a PDF renamed .png"),
        ("pptx", DOCX, "a docx renamed .pptx"),
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


def test_sniffing_works_on_a_truncated_head():
    """Only the head is read from the upload stream, so detection must not
    depend on trailing bytes — an OOXML archive's central directory is at the
    end of the file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/media/image1.png", os.urandom(300_000))  # incompressible
        zf.writestr("word/document.xml", "<w:document/>")
    full = buf.getvalue()
    assert len(full) > CONTENT_SNIFF_BYTES
    validate_content_matches_extension("docx", full[:CONTENT_SNIFF_BYTES])


def test_error_names_the_extension_and_what_was_found():
    with pytest.raises(ValidationError) as exc_info:
        validate_content_matches_extension("pdf", PNG)
    message = str(exc_info.value)
    assert ".pdf" in message and "png" in message


def test_real_pdf_fixture_is_accepted():
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[3] / "resources" / "test_file.pdf"
    validate_content_matches_extension("pdf", fixture.read_bytes()[:CONTENT_SNIFF_BYTES])
