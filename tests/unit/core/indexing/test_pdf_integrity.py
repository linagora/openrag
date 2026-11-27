"""A PDF that cannot be opened is the file's fault, said with a code and no server path (#1490)."""

from __future__ import annotations

import pickle

import pymupdf
import pypdfium2
import pypdfium2.raw as pdfium_c
import pytest
from core.indexing import pdf_integrity
from core.indexing.pdf_integrity import (
    CORRUPT_PDF_MESSAGE,
    ENCRYPTED_PDF_MESSAGE,
    check_pdf_opens,
    open_mupdf_document,
    pdfium_page_count,
)
from core.utils.exceptions import InvalidDocumentError, ValidationError


def _write(tmp_path, data: bytes, name: str = "a1b2c3_report.pdf"):
    path = tmp_path / "data" / "partition" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


def test_a_sound_pdf_opens_in_both_libraries(tmp_path, pdf_samples):
    path = _write(tmp_path, pdf_samples.sound)

    assert pdfium_page_count(path) == 1
    with open_mupdf_document(str(path)) as doc:
        assert doc.page_count == 1
    check_pdf_opens(path)


def test_a_truncated_pdf_is_an_invalid_pdf_whose_message_names_no_path(tmp_path, pdf_samples):
    """The issue's case: a valid PDF cut to its first 4 KiB, from a path under the data dir."""
    path = _write(tmp_path, pdf_samples.truncated)

    with pytest.raises(InvalidDocumentError) as raised:
        pdfium_page_count(str(path))

    error = raised.value
    assert error.code == "INVALID_PDF"
    assert error.status_code == 422
    assert error.message == CORRUPT_PDF_MESSAGE
    assert str(tmp_path) not in str(error) and "/" not in error.message
    # Logged at WARNING by the worker, like any invalid input, not as an operator alert.
    assert isinstance(error, ValidationError)


def test_an_encrypted_pdf_says_so_in_both_libraries(pdf_samples):
    with pytest.raises(InvalidDocumentError) as from_pdfium:
        pdfium_page_count(pdf_samples.encrypted)
    with pytest.raises(InvalidDocumentError) as from_mupdf:
        open_mupdf_document(pdf_samples.encrypted)

    for raised in (from_pdfium, from_mupdf):
        assert raised.value.code == "ENCRYPTED_PDF"
        assert raised.value.message == ENCRYPTED_PDF_MESSAGE


def test_a_pdf_mupdf_cannot_open_is_an_invalid_pdf():
    with pytest.raises(InvalidDocumentError) as raised:
        open_mupdf_document(b"%PDF-1.7\n" + b"\x00" * 512)

    assert raised.value.code == "INVALID_PDF"


def test_pdfium_failing_for_another_reason_is_raised_as_it_is(monkeypatch):
    """Only what the file is to blame for becomes ``InvalidDocumentError``: a
    read error may pass on a retry, so it must not be reported as the file's."""

    def unreadable(_source):
        raise pypdfium2.PdfiumError("Failed to load document (PDFium: File access error).")

    monkeypatch.setattr(pdf_integrity.pypdfium2, "PdfDocument", unreadable)
    monkeypatch.setattr(pdf_integrity.pdfium_c, "FPDF_GetLastError", lambda: pdfium_c.FPDF_ERR_FILE)

    with pytest.raises(pypdfium2.PdfiumError):
        pdfium_page_count("report.pdf")


def test_the_upload_check_refuses_a_pdf_neither_library_opens(tmp_path, pdf_samples):
    path = _write(tmp_path, pdf_samples.truncated)

    with pytest.raises(InvalidDocumentError) as raised:
        check_pdf_opens(path)

    assert raised.value.code == "INVALID_PDF"
    assert str(tmp_path) not in str(raised.value)


def test_the_upload_check_refuses_an_encrypted_pdf(tmp_path, pdf_samples):
    with pytest.raises(InvalidDocumentError) as raised:
        check_pdf_opens(_write(tmp_path, pdf_samples.encrypted))

    assert raised.value.code == "ENCRYPTED_PDF"


def test_the_upload_check_accepts_a_pdf_only_mupdf_can_read(tmp_path, pdf_samples):
    """PyMuPDF, the default loader, repairs files pdfium refuses: this one loses
    its xref table and trailer and still reads in full. Refusing it at upload
    would lose a file the default deployment indexes."""
    path = _write(tmp_path, pdf_samples.tail_cut)
    with pytest.raises(InvalidDocumentError):
        pdfium_page_count(path)
    with open_mupdf_document(str(path)) as doc:
        assert "Readable text" in doc[0].get_text()

    check_pdf_opens(path)


def test_the_upload_check_lets_through_what_is_not_the_file_s_fault(tmp_path, monkeypatch, pdf_samples):
    path = _write(tmp_path, pdf_samples.truncated)

    def unreadable(_source):
        raise OSError("disk went away")

    monkeypatch.setattr(pdf_integrity, "pdfium_page_count", unreadable)

    check_pdf_opens(path)


def test_the_upload_check_does_not_keep_mupdf_s_warnings(tmp_path, pdf_samples):
    """PyMuPDF keeps every warning in a list for the life of the process: the API
    would grow it by a few lines per broken PDF uploaded."""
    pymupdf.TOOLS.reset_mupdf_warnings()
    path = _write(tmp_path, pdf_samples.truncated)

    with pytest.raises(InvalidDocumentError):
        check_pdf_opens(path)

    assert pymupdf.TOOLS.mupdf_warnings() == ""


def test_the_error_crosses_a_process_as_it_was_raised():
    """Ray and the PyMuPDF pool pickle it. Rebuilt from ``args`` ("CODE: message"),
    its code would be prefixed a second time."""
    error = InvalidDocumentError(CORRUPT_PDF_MESSAGE, code="INVALID_PDF")

    copy = pickle.loads(pickle.dumps(error))

    assert type(copy) is InvalidDocumentError
    assert (copy.code, copy.message, copy.status_code) == ("INVALID_PDF", CORRUPT_PDF_MESSAGE, 422)
    assert str(copy) == str(error) == f"INVALID_PDF: {CORRUPT_PDF_MESSAGE}"
