"""Object keys for uploaded files: the one module that builds and parses them.

An uploaded file lives in an object store under a key. No other code
concatenates a key or turns an identifier into a path: it calls a builder
here, and the store adapters accept only keys :func:`parse_key` can read.

==============  ======================================================  =====================================
Prefix          Key                                                     Holds
==============  ======================================================  =====================================
``originals/``  ``originals/{partition}/{file_id}/{upload_id}{suffix}``  an upload kept after indexing
``uploads/``    ``uploads/{upload_id}{suffix}``                         an upload deleted once its job settles
``tmp/``        ``tmp/{uuid}{suffix}``                                  a file read once, then deleted
``legacy/``     ``legacy/{path relative to data_dir}``                  a file stored before the object store
==============  ======================================================  =====================================

How a key is made safe:

* ``partition`` and ``file_id`` are written as URL-safe base64 without
  padding. The alphabet has neither ``/`` nor ``.``, so a file id of ``..``
  or ``a/b`` cannot leave its prefix or collide with another id. The
  encoding is canonical: one id, one component.
* ``upload_id`` is 32 lowercase hex digits (:func:`new_upload_id`): one
  object per revision, so a replace never overwrites the object a running
  job reads.
* The suffix is the one the file is parsed as (``.pdf``), so a path-based
  parser can open the stored file as it is. It is the only part of the
  filename in the key; the filename itself travels as object metadata.
* A legacy path has no empty, ``.``, ``..`` or hidden (``.name``) part: the
  filesystem store keeps its own files under hidden names.
* No component is longer than 255 bytes (a filesystem name limit) and no
  key longer than 1,024 bytes (S3's limit). A partition or file id over
  191 bytes encodes past 255 and is refused.

A key can also name the markdown copy of a parse: ``{key}.md``
(:func:`markdown_sidecar_key`).
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath

from .exceptions import InvalidObjectKeyError

ORIGINALS = "originals"
UPLOADS = "uploads"
TMP = "tmp"
LEGACY = "legacy"
PREFIXES = frozenset({ORIGINALS, UPLOADS, TMP, LEGACY})

MAX_KEY_BYTES = 1024
MAX_COMPONENT_BYTES = 255
MARKDOWN_SIDECAR_SUFFIX = ".md"

_HEX_ID_RE = re.compile(r"[0-9a-f]{32}")
_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,16}")
_B64_COMPONENT_RE = re.compile(r"[A-Za-z0-9_-]+")
# The last component of an originals/, uploads/ or tmp/ key: the id, the parse
# suffix, then ``.md`` for a markdown sidecar.
_LEAF_RE = re.compile(r"(?P<id>[0-9a-f]{32})(?P<suffix>\.[A-Za-z0-9]{1,16})?(?P<sidecar>\.md)?")


@dataclass(frozen=True, slots=True)
class ParsedKey:
    """What a key names. Fields that don't apply to its prefix are ``None``."""

    prefix: str
    partition: str | None = None
    file_id: str | None = None
    # The upload_id of an originals/ or uploads/ key, the uuid of a tmp/ key.
    upload_id: str | None = None
    suffix: str = ""
    # True for ``{key}.md`` of an originals/ or uploads/ key. A legacy/ path
    # ending in ``.md`` is a markdown upload as much as a sidecar, so it is
    # never flagged.
    markdown_sidecar: bool = False
    # The path under data_dir of a legacy/ key.
    relative_path: str | None = None


def new_upload_id() -> str:
    """A fresh id for one stored revision of an upload."""
    return uuid.uuid4().hex


def encode_component(value: str) -> str:
    """Encode a partition or file id as one key component."""
    if not isinstance(value, str) or not value:
        raise InvalidObjectKeyError("An object key component cannot be empty.")
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")
    _check_component_length(encoded)
    return encoded


def decode_component(encoded: str) -> str:
    """Decode a component written by :func:`encode_component`.

    Refuses anything :func:`encode_component` would not have written,
    non-canonical spellings of the same bytes included.
    """
    if not isinstance(encoded, str) or _B64_COMPONENT_RE.fullmatch(encoded) is None:
        raise InvalidObjectKeyError("Not an encoded object key component.")
    try:
        value = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidObjectKeyError("Not an encoded object key component.") from exc
    if not value or encode_component(value) != encoded:
        raise InvalidObjectKeyError("Not an encoded object key component.")
    return value


def original_key(partition: str, file_id: str, upload_id: str, suffix: str = "") -> str:
    """The key of an upload kept after indexing."""
    leaf = _leaf(upload_id, suffix)
    return _finish(f"{ORIGINALS}/{encode_component(partition)}/{encode_component(file_id)}/{leaf}")


def upload_key(upload_id: str, suffix: str = "") -> str:
    """The key of an upload that is deleted once its job settles."""
    return _finish(f"{UPLOADS}/{_leaf(upload_id, suffix)}")


def tmp_key(suffix: str = "") -> str:
    """A fresh key for a file read once, then deleted."""
    return _finish(f"{TMP}/{_leaf(uuid.uuid4().hex, suffix)}")


def legacy_key(relative_path: str | PurePath) -> str:
    """The key of a file stored under data_dir before the object store existed."""
    return _finish(f"{LEGACY}/{_check_relative_path(relative_path)}")


def legacy_key_for_source(source: str | PurePath, data_dir: str | PurePath) -> str | None:
    """The legacy/ key of a ``source`` path, or ``None`` when it is not under ``data_dir``.

    Lexical: both paths are taken as given (the upload routes store ``source``
    resolved), so pass ``data_dir`` resolved too.
    """
    try:
        relative = PurePosixPath(source).relative_to(PurePosixPath(data_dir))
    except ValueError:
        return None
    if not relative.parts:
        return None
    try:
        return legacy_key(relative)
    except InvalidObjectKeyError:
        return None


def markdown_sidecar_key(key: str) -> str:
    """The key of the markdown copy of an upload's parse."""
    parsed = parse_key(key)
    if parsed.prefix == TMP or parsed.markdown_sidecar:
        raise InvalidObjectKeyError("Only an originals/, uploads/ or legacy/ object has a markdown sidecar.")
    if parsed.prefix != LEGACY and not parsed.suffix:
        # ``{upload_id}`` + ``.md`` would read back as a markdown upload.
        raise InvalidObjectKeyError("Only an object stored under a suffix has a markdown sidecar.")
    # Checked whole: a legacy name near 255 bytes has no room for ``.md``.
    return validate_key(key + MARKDOWN_SIDECAR_SUFFIX)


def validate_suffix(suffix: str) -> str:
    """Return ``suffix`` if it is empty or a dot and 1 to 16 letters or digits, else raise.

    The rule for the suffix a key ends with, and for the one a store's
    ``local_copy`` is asked to give its path: it can't hold a separator.
    """
    if not isinstance(suffix, str) or (suffix and _SUFFIX_RE.fullmatch(suffix) is None):
        raise InvalidObjectKeyError("A suffix is a dot followed by 1 to 16 letters or digits.")
    return suffix


def validate_key(key: str) -> str:
    """Return ``key`` if this module could have built it, else raise ``InvalidObjectKeyError``."""
    parse_key(key)
    return key


def parse_key(key: str) -> ParsedKey:
    """Read a key back into what it names. Refuses any key the builders here can't produce."""
    if not isinstance(key, str) or not key:
        raise InvalidObjectKeyError("An object key cannot be empty.")
    _check_key_length(key)
    prefix, sep, rest = key.partition("/")
    if not sep or prefix not in PREFIXES:
        raise InvalidObjectKeyError("Unknown object key prefix.")
    if prefix == LEGACY:
        return ParsedKey(prefix=LEGACY, relative_path=_check_relative_path(rest))

    parts = rest.split("/")
    if prefix == ORIGINALS:
        if len(parts) != 3:
            raise InvalidObjectKeyError("An originals/ key has a partition, a file id and an upload id.")
        partition, file_id = decode_component(parts[0]), decode_component(parts[1])
    else:
        if len(parts) != 1:
            raise InvalidObjectKeyError(f"A {prefix}/ key has a single component.")
        partition = file_id = None
    match = _LEAF_RE.fullmatch(parts[-1])
    if match is None:
        raise InvalidObjectKeyError("Malformed object id or suffix.")
    sidecar = match["sidecar"] is not None
    if sidecar and prefix == TMP:
        raise InvalidObjectKeyError("A tmp/ object has no markdown sidecar.")
    return ParsedKey(
        prefix=prefix,
        partition=partition,
        file_id=file_id,
        upload_id=match["id"],
        suffix=match["suffix"] or "",
        markdown_sidecar=sidecar,
    )


def _leaf(object_id: str, suffix: str) -> str:
    if not isinstance(object_id, str) or _HEX_ID_RE.fullmatch(object_id) is None:
        raise InvalidObjectKeyError("An upload id is 32 lowercase hex digits.")
    return object_id + validate_suffix(suffix)


def _check_relative_path(relative_path: str | PurePath) -> str:
    """Check a legacy path component by component and return it with ``/`` separators."""
    text = relative_path.as_posix() if isinstance(relative_path, PurePath) else relative_path
    if not isinstance(text, str) or not text:
        raise InvalidObjectKeyError("A legacy path cannot be empty.")
    if "\\" in text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise InvalidObjectKeyError("A legacy path cannot hold a backslash or a control character.")
    parts = text.split("/")
    if parts[0] in PREFIXES:
        # data_dir/originals/... is where the filesystem store keeps new
        # objects: a legacy key must not reach them under another prefix.
        raise InvalidObjectKeyError("A legacy path cannot start with an object key prefix.")
    for part in parts:
        if not part or part.startswith("."):
            # Covers "." and "..", and the hidden names a store keeps its own
            # files under. Uploads were always written under a timestamp.
            raise InvalidObjectKeyError("A legacy path must be relative, without empty or hidden ('.') parts.")
        _check_component_length(part)
    return text


def _check_component_length(component: str) -> None:
    if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise InvalidObjectKeyError(f"An object key component is longer than {MAX_COMPONENT_BYTES} bytes.")


def _check_key_length(key: str) -> None:
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise InvalidObjectKeyError(f"An object key is longer than {MAX_KEY_BYTES} bytes.")


def _finish(key: str) -> str:
    _check_key_length(key)
    return key


__all__ = [
    "LEGACY",
    "MARKDOWN_SIDECAR_SUFFIX",
    "MAX_COMPONENT_BYTES",
    "MAX_KEY_BYTES",
    "ORIGINALS",
    "PREFIXES",
    "TMP",
    "UPLOADS",
    "ParsedKey",
    "decode_component",
    "encode_component",
    "legacy_key",
    "legacy_key_for_source",
    "markdown_sidecar_key",
    "new_upload_id",
    "original_key",
    "parse_key",
    "tmp_key",
    "upload_key",
    "validate_key",
    "validate_suffix",
]
