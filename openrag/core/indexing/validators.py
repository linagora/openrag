"""Framework-free validators for indexing inputs.

Pure functions on plain types — no FastAPI, no Hydra. Routers translate
incoming HTTP requests into these inputs and let the global ``OpenRAGError``
handler convert raised ``ValidationError`` instances into HTTP responses.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable
from typing import IO, Any

import filetype

from ..utils.exceptions import ValidationError

DEFAULT_FORBIDDEN_CHARS_IN_FILE_ID: frozenset[str] = frozenset("/")

# Identifiers (file_id, partition name) are interpolated into Milvus filter
# expression strings (e.g. ``file_id == "..."``). Even though the store escapes
# values, restrict identifiers to a safe allowlist as defence-in-depth and input
# hygiene so quotes / brackets / operators can never reach a filter literal.
_VALID_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:\-]+")


def parse_metadata(raw: Any | None) -> dict:
    """Parse JSON-encoded metadata into a dict.

    Accepts ``None`` / empty string (returns ``{}``), an existing dict
    (returned as-is), or a JSON string that decodes to a dict.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError("Invalid JSON in metadata", status_code=400) from exc
    if not isinstance(decoded, dict):
        raise ValidationError("Metadata must be a JSON object", status_code=400)
    return decoded


def validate_file_id(
    file_id: str,
    forbidden_chars: Iterable[str] = DEFAULT_FORBIDDEN_CHARS_IN_FILE_ID,
) -> str:
    """Return normalized ``file_id`` if valid, else raise ``ValidationError`` (HTTP 400)."""
    if not isinstance(file_id, str):
        raise ValidationError("File ID must be a string.", status_code=400)
    file_id = file_id.strip()
    if not file_id:
        raise ValidationError("File ID cannot be empty.", status_code=400)
    if _VALID_IDENTIFIER_RE.fullmatch(file_id) is None:
        raise ValidationError(
            "File ID may only contain letters, digits, '.', '_', ':' and '-'.",
            status_code=400,
        )
    if any(char in file_id for char in forbidden_chars):
        raise ValidationError("File ID contains forbidden characters.", status_code=400)
    return file_id


def validate_partition_name(partition: str) -> str:
    """Return ``partition`` if it is a safe identifier, else raise ``ValidationError``.

    Used on partition creation and on every partition-scoped operation so a
    crafted name can never reach a Milvus filter expression string.
    """
    if not isinstance(partition, str) or not partition or _VALID_IDENTIFIER_RE.fullmatch(partition) is None:
        raise ValidationError(
            "Partition name may only contain letters, digits, '.', '_', ':' and '-'.",
            status_code=400,
        )
    return partition


def validate_file_format(
    filename: str | None,
    accepted_formats: Iterable[str],
    accepted_mimetypes: Iterable[str],
    mimetype: str | None = None,
) -> str:
    """Validate the file by extension or mimetype.

    Returns the lowercased file extension (without the leading dot, possibly
    empty). Raises ``ValidationError`` (HTTP 415) on rejection, or
    ``ValidationError`` (HTTP 422) when the filename is missing from the
    multipart upload.
    """
    if not filename:
        raise ValidationError(
            "Uploaded file part has no filename.",
            status_code=422,
        )
    file_extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    formats = set(accepted_formats)
    mimetypes = set(accepted_mimetypes)
    if file_extension not in formats and mimetype not in mimetypes:
        details = (
            f"Unsupported file format: {file_extension} or file mimetype.\n"
            f"Supported formats: {', '.join(sorted(formats))}\n"
            f"Supported mimetypes: {', '.join(sorted(mimetypes))}"
        )
        raise ValidationError(details, status_code=415)
    return file_extension


#: Bytes read from the head of an upload for signature detection. The matchers
#: that need the most read a few hundred bytes; 8 KiB is well clear of that and
#: is read once per upload.
CONTENT_SNIFF_BYTES = 8192

#: Extensions whose content carries a signature we can check, mapped to what
#: ``filetype`` reports for that signature.
#:
#: An extension absent from this map is not verified, and that is deliberate
#: rather than an oversight:
#:
#: * ``txt``/``md``/``html``/``htm``/``eml``/``svg`` are text formats with no
#:   signature to check.
#: * ``wma`` was verified empirically against the bundled matchers and is not
#:   reliably detected, so enforcing it would reject legitimate uploads.
#: * ``doc`` is **not** here either: its parser accepts several formats, so it
#:   is checked against :data:`_TOLERANT_SIGNATURES` instead.
#: * Audio and video containers other than those above are left out until the
#:   accepted brand variants can be checked against real samples; guessing at
#:   them risks refusing valid media.
#: * ``docx``/``pptx`` are **not** here: a ZIP's authoritative index is its
#:   central directory, which sits at the end of the file, so no head buffer can
#:   settle them. They are checked by :func:`validate_ooxml_package` instead.
_VERIFIABLE_SIGNATURES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "png": frozenset({"png"}),
    "jpg": frozenset({"jpg"}),
    "jpeg": frozenset({"jpg"}),
    "gif": frozenset({"gif"}),
    "bmp": frozenset({"bmp"}),
    "webp": frozenset({"webp"}),
}

#: Extensions whose parser legitimately consumes several formats, so the check
#: refuses what ``filetype`` recognises as *something else* rather than
#: requiring a signature of its own. The value is what stays acceptable beyond
#: "no signature at all".
#:
#: ``doc`` is the only one, and it is the exception to the allowlist above for a
#: reason that is a property of the format, not a shortcut. Spire.Doc loads
#: OLE2, RTF, HTML and plain text under a ``.doc`` name — all four were verified
#: loading — and Word has historically written all of them that way. OLE2 and
#: HTML/text are indistinguishable to ``filetype`` (it reports ``None`` for
#: each), so requiring a known-good signature would refuse legitimate uploads.
#: That is exactly why ``.doc`` was excluded from #957 and became the only
#: accepted format with no check at all (#964).
#:
#: What this does close: a renamed PDF, ZIP/OOXML, image, archive or executable
#: reaching Spire. Those are the parser-bomb vectors, and ``filetype`` names
#: every one of them.
#:
#: What it deliberately does not: arbitrary *unsignatured* bytes still reach
#: Spire, where they fail the load and fall back to ``GetText()`` — the path
#: ``DocParser`` already has. Nor does it stop a crafted ``.doc``, which carries
#: a real document's signature; bounding that is #997's job. Narrowing this to
#: an allowlist (OLE2 + RTF only) is possible once someone confirms no legacy
#: HTML/text ``.doc`` files exist in the corpora — a corpus question, not a code
#: one.
_TOLERANT_SIGNATURES: dict[str, frozenset[str]] = {
    # ``docx`` is here because Spire loads an OOXML package under a ``.doc``
    # name and extracts it — a .docx saved or renamed as .doc indexes today, and
    # refusing it would be the regression this rule exists to avoid. It does not
    # escape the package check: ``validate_ooxml_package`` settles a tolerant
    # extension by content, so such a file is checked as the docx it is.
    #
    # ``doc`` is here as well as ``rtf``: ``filetype`` *does* recognise a real
    # Word 97-2003 document — the FIB marker at offset 512, or the
    # ``Word.Document.8`` string at 2075-2142 — and omitting it would 415 the
    # one file this extension exists for. It stays unreliable in the other
    # direction: an OLE2 document without either marker reports ``None``, which
    # is why the rule cannot simply require ``doc``.
    "doc": frozenset({"doc", "docx", "rtf"}),
}

#: The part whose presence makes an OPC package a document of that kind, per
#: ECMA-376.
_OOXML_MAIN_PARTS: dict[str, str] = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
}

#: Parts every OPC package carries whatever its flavour: the content-type map
#: and the package relationships. Both are mandatory, and an archive that only
#: borrowed a document's entry names has neither.
_OOXML_PACKAGE_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels"})


def _ooxml_main_part_by_content(extension: str, stream: IO[bytes]) -> str | None:
    """The OOXML main part implied by a tolerant extension's *content*, if any.

    Only the tolerant extensions reach here: everything else is settled by the
    name, and an extension that is not tolerant never accepted foreign content
    in the first place. The stream position is restored either way, since the
    caller goes on to read the same handle.
    """
    if extension not in _TOLERANT_SIGNATURES:
        return None
    position = stream.tell()
    try:
        kind = filetype.guess(stream.read(CONTENT_SNIFF_BYTES))
    finally:
        stream.seek(position)
    return _OOXML_MAIN_PARTS.get(kind.extension) if kind is not None else None


def validate_content_matches_extension(extension: str, head: bytes) -> None:
    """Reject an upload whose bytes contradict the extension it was named with.

    The extension alone decides which parser a document reaches, so a file
    renamed to ``.pdf`` is handed to the PDF backend whatever it actually
    contains. For the formats in :data:`_VERIFIABLE_SIGNATURES` the signature
    ``filetype`` reports must match; an unrecognised signature is a failure too,
    because arbitrary content is exactly what this rejects. The formats in
    :data:`_TOLERANT_SIGNATURES` invert that: their parser accepts several
    formats, so anything ``filetype`` recognises as *something else* is refused
    and everything it cannot place is allowed.

    Extensions outside that map pass through untouched — there is nothing to
    check, and refusing them would be a guess.

    Raises:
        ValidationError: HTTP 415, when the content contradicts the extension.
    """
    tolerated = _TOLERANT_SIGNATURES.get(extension)
    if tolerated is not None:
        kind = filetype.guess(head)
        # ``None`` covers OLE2, HTML and plain text alike — all three load.
        if kind is None or kind.extension in tolerated:
            return
        raise ValidationError(
            f"Uploaded file does not match its .{extension} extension: it looks like a "
            f"{kind.extension} file. Upload it with the extension matching its actual format.",
            status_code=415,
        )

    expected = _VERIFIABLE_SIGNATURES.get(extension)
    if expected is None:
        return

    kind = filetype.guess(head)
    detected = kind.extension if kind is not None else None
    if detected in expected:
        return

    found = f"looks like a {detected} file" if detected else f"is not a recognised {extension} file"
    raise ValidationError(
        f"Uploaded file does not match its .{extension} extension: it {found}. "
        f"Upload it with the extension matching its actual format.",
        status_code=415,
    )


def validate_ooxml_package(extension: str, stream: IO[bytes]) -> None:
    """Reject a ``.docx``/``.pptx`` upload that is not a real OOXML package.

    ``filetype`` cannot settle this, and was verified failing both ways: its
    matcher looks for an entry *named* ``word/`` or ``ppt/`` among the first few
    ZIP local file headers, so a plain archive containing ``word/anything.txt``
    is reported as a docx, while a genuine document whose ``customXml``/
    ``docProps`` parts are written first is reported as a plain zip and would be
    refused. The package is opened instead and its central directory — the
    authoritative index, at the end of the file — is read.

    Only the directory is parsed; no member is decompressed, so a compression
    bomb is never expanded here.

    Blocking by design, like the parsers in ``core/indexing/parsers/``. Callers
    on the event loop wrap it in ``asyncio.to_thread``: reading the directory of
    an attacker-supplied archive is unbounded work, and the API serves streaming
    responses and ``/health_check`` from the same loop.

    Args:
        extension: Lowercased extension without the dot. Anything that is not an
            OOXML format returns without reading the stream.
        stream: Seekable binary stream holding the **whole** file. Its position
            is restored before returning, on success and on rejection alike, so
            a caller can go on to stream the same handle to disk.

    Raises:
        ValidationError: HTTP 415, when the archive is not a package of that kind.
    """
    main_part = _OOXML_MAIN_PARTS.get(extension)
    if main_part is None:
        # A tolerant extension can still carry an OOXML package: a .docx saved as
        # .doc is accepted by the head check, because Spire reads it. Settle this
        # one by content so it meets the same package check a .docx upload does —
        # otherwise the extension is a way around it.
        main_part = _ooxml_main_part_by_content(extension, stream)
        if main_part is None:
            return

    position = stream.tell()
    try:
        with zipfile.ZipFile(stream) as package:
            names = set(package.namelist())
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ValidationError(
            f"Uploaded file does not match its .{extension} extension: it is not a readable "
            f"Office package. Upload it with the extension matching its actual format.",
            status_code=415,
        ) from exc
    finally:
        stream.seek(position)

    missing = (_OOXML_PACKAGE_PARTS | {main_part}) - names
    if missing:
        raise ValidationError(
            f"Uploaded file does not match its .{extension} extension: the archive is missing "
            f"{', '.join(sorted(missing))}, so it is not a valid Office package. "
            f"Upload it with the extension matching its actual format.",
            status_code=415,
        )
