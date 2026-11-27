"""Whether a PDF opens, and the typed error to raise when it does not.

A PDF that cannot be opened (a truncated upload, a partial download) or that is
locked by a password fails the same way on every attempt. That is the file's
outcome, not a fault of the service, so it is raised as
:class:`InvalidDocumentError` (422): never retried, and with a message that names
no server path.

Two libraries read PDFs here: pdfium, under Marker, Docling and DotsOCR, and
MuPDF, under PyMuPDF, the default loader. MuPDF repairs files pdfium refuses: a
PDF that lost its cross-reference table, even one cut in half, can still open in
MuPDF with every page, and not in pdfium. So each loader judges a file with the
library it parses it with, and the upload check refuses only what neither opens.
"""

from __future__ import annotations

import os
import threading

import pymupdf
import pypdfium2
import pypdfium2.raw as pdfium_c

from ..utils.exceptions import InvalidDocumentError
from ..utils.logging import get_logger

logger = get_logger()

CORRUPT_PDF_MESSAGE = "The PDF could not be opened: the file is corrupt or truncated."
ENCRYPTED_PDF_MESSAGE = "This PDF is password-protected or encrypted. Remove the password and upload it again."

_PDFIUM_ENCRYPTED = frozenset({pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY})
# ``SUCCESS`` too: pypdfium2 also refuses a document that loads with no page.
_PDFIUM_CORRUPT = frozenset({pdfium_c.FPDF_ERR_FORMAT, pdfium_c.FPDF_ERR_SUCCESS})

# Neither library is thread-safe, and both ``check_pdf_opens`` (in the API, one
# thread per upload) and ``pdfium_page_count`` (in the Marker and Docling pools,
# off their event loop) run in threads. Reentrant: the check counts pages too.
# The loaders that parse open the file on the one thread that parses it.
_LOCK = threading.RLock()


def corrupt_pdf_error() -> InvalidDocumentError:
    return InvalidDocumentError(CORRUPT_PDF_MESSAGE, code="INVALID_PDF")


def encrypted_pdf_error() -> InvalidDocumentError:
    return InvalidDocumentError(ENCRYPTED_PDF_MESSAGE, code="ENCRYPTED_PDF")


def _pdfium_refusal(exc: pypdfium2.PdfiumError) -> InvalidDocumentError | None:
    """What pdfium failing to open a document says about the file, if anything.

    ``None`` when the file is not the reason (pdfium could not read it, say),
    which may pass on another attempt. pdfium keeps one last error for the
    process, so this is read right after the failure, on the thread that saw it.
    """
    code = getattr(exc, "err_code", None)
    if code is None:
        code = pdfium_c.FPDF_GetLastError()
    if code in _PDFIUM_ENCRYPTED:
        return encrypted_pdf_error()
    if code in _PDFIUM_CORRUPT:
        return corrupt_pdf_error()
    return None


def open_pdfium_document(source: str | os.PathLike[str] | bytes) -> pypdfium2.PdfDocument:
    """Open *source* with pdfium; the caller closes the document.

    Raises:
        InvalidDocumentError: the file is corrupt, truncated or encrypted.
        pypdfium2.PdfiumError: pdfium failed for another reason.
    """
    try:
        return pypdfium2.PdfDocument(source)
    except pypdfium2.PdfiumError as exc:
        refusal = _pdfium_refusal(exc)
        if refusal is None:
            raise
        raise refusal from exc


def pdfium_page_count(source: str | os.PathLike[str] | bytes) -> int:
    """The page count of *source*, opened with pdfium; raises as :func:`open_pdfium_document`.

    Blocking, and safe to run in a thread: pdfium scans a damaged file whole,
    about 6 ms per MB.
    """
    with _LOCK:
        pdf = open_pdfium_document(source)
        try:
            return len(pdf)
        finally:
            pdf.close()


def open_mupdf_document(source: str | os.PathLike[str] | bytes) -> pymupdf.Document:
    """Open *source* (a path, or the PDF's bytes) with MuPDF; the caller closes it.

    Raises:
        InvalidDocumentError: MuPDF cannot open the file even after repairing it,
            or the file needs a password.
    """
    try:
        if isinstance(source, bytes):
            doc = pymupdf.open(stream=source, filetype="pdf")
        else:
            doc = pymupdf.open(source, filetype="pdf")
    except pymupdf.FileDataError as exc:
        raise corrupt_pdf_error() from exc
    if doc.needs_pass:
        doc.close()
        raise encrypted_pdf_error()
    return doc


def _mupdf_refusal(path: str) -> InvalidDocumentError | None:
    """Why MuPDF cannot read the PDF at *path*, or ``None`` when it can.

    A document MuPDF opens with no page is refused too: no loader would find
    anything in it. PyMuPDF appends every warning a repair logs to a list it
    keeps for the life of the process; the API process has nothing to read them
    for, so they are dropped.
    """
    try:
        with open_mupdf_document(path) as doc:
            return None if doc.page_count else corrupt_pdf_error()
    except InvalidDocumentError as exc:
        return exc
    finally:
        pymupdf.TOOLS.reset_mupdf_warnings()


def check_pdf_opens(path: str | os.PathLike[str]) -> None:
    """Refuse a PDF that no PDF loader here can open (blocking: run it in a thread).

    pdfium goes first: it opens a sound file from its cross-reference table, in
    milliseconds whatever the size. Only a file it refuses goes to MuPDF, which
    repairs what it can; the file is refused when MuPDF fails too, finds no page,
    or needs a password. Each library scans a broken file whole (about 6 ms per
    MB), reading it from disk rather than into memory.

    Any other failure lets the file through, to the loader that will parse it:
    this check only refuses what it can tell is the file's fault.

    Raises:
        InvalidDocumentError: when neither library can open the file.
    """
    path = os.fspath(path)
    with _LOCK:
        try:
            pdfium_page_count(path)
            return
        except InvalidDocumentError:
            pass
        except Exception as exc:  # noqa: BLE001 - not the file's fault; the loader decides
            logger.bind(error=str(exc)).warning("Could not check that a PDF opens; accepting it.")
            return
        try:
            refusal = _mupdf_refusal(path)
        except Exception as exc:  # noqa: BLE001 - not the file's fault; the loader decides
            logger.bind(error=str(exc)).warning("Could not check that a PDF opens; accepting it.")
            return
    if refusal is not None:
        raise refusal
