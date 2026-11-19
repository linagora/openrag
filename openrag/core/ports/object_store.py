"""Object store port: where uploaded files are kept.

Every read and write of an uploaded file goes through an :class:`ObjectStore`,
under a key built by :mod:`core.utils.object_keys`. The adapters live in
``services/storage/``: one on the filesystem (``data_dir``), one on any
S3-compatible store. All of them pass the same conformance suite
(``tests/unit/services/storage/object_store_conformance.py``), which is the
executable form of the contract written here.

The contract, for every adapter:

* **Keys.** A method refuses a key :func:`core.utils.object_keys.validate_key`
  refuses, with ``InvalidObjectKeyError``, before any I/O.
* **Writes are whole.** A reader sees an object entirely or not at all. A
  ``put_stream`` whose stream raises, or that is cancelled, leaves the key as
  it was: no partial object, and any previous object still there.
* **Missing objects.** ``open_stream``, ``local_copy``, ``stat`` and ``copy``
  raise ``ObjectNotFoundError``. ``delete`` and ``delete_many`` treat a
  missing key as already deleted.
* **Metadata** is a small ``str -> str`` mapping stored with the object and
  returned by ``stat`` exactly as given, non-ASCII values included
  (:func:`validate_metadata` has the rules). ``copy`` carries it over.
* **Failures.** Anything else the backend reports is an ``ObjectStoreError``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from core.utils.exceptions import InvalidByteRangeError, ValidationError

# S3 keeps 2 KiB of user metadata, and its headers are ASCII: the S3 adapter
# percent-encodes values, which can triple them, and adds its own entries
# (the SHA-256, under the reserved prefix). 640 UTF-8 bytes, tripled, still
# leave room for those.
MAX_METADATA_BYTES = 640
RESERVED_METADATA_PREFIX = "openrag-"
_METADATA_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """What a store knows about one object."""

    key: str
    size: int
    # Hex SHA-256 of the content. Always set by ``put_stream``; ``copy`` and
    # ``stat`` give the one known for the object, ``None`` for a file the port
    # didn't write (stored before the object store existed).
    sha256: str | None
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Bytes ``start`` to ``end`` of an object, both included, as in an HTTP ``Range``.

    ``end=None`` reads to the end. An ``end`` past the end of the object is
    cut to it; a ``start`` at or past the end raises ``InvalidByteRangeError``
    when the object is opened.
    """

    start: int
    end: int | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or (self.end is not None and self.end < self.start):
            raise InvalidByteRangeError(f"Invalid byte range {self.start}-{self.end}.")

    def resolve(self, size: int) -> tuple[int, int]:
        """The ``(offset, length)`` this range reads from an object of ``size`` bytes."""
        if self.start >= size:
            raise InvalidByteRangeError(f"Byte range starts at {self.start}, past the end of a {size}-byte object.")
        last = size - 1 if self.end is None else min(self.end, size - 1)
        return self.start, last - self.start + 1


def validate_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Return a copy of ``metadata`` if a store can keep it, else raise ``ValidationError``.

    Keys are lowercase letters, digits and ``-`` (what S3 keeps unchanged),
    up to 64 characters, and don't start with :data:`RESERVED_METADATA_PREFIX`,
    which adapters keep for their own entries. Values are any string. Keys and
    values together take at most :data:`MAX_METADATA_BYTES` in UTF-8.
    """
    if metadata is None:
        return {}
    checked: dict[str, str] = {}
    total = 0
    for key, value in metadata.items():
        if not isinstance(key, str) or _METADATA_KEY_RE.fullmatch(key) is None:
            raise ValidationError(f"Invalid object metadata key {key!r}.", status_code=400)
        if key.startswith(RESERVED_METADATA_PREFIX):
            raise ValidationError(f"Object metadata key {key!r} uses a reserved prefix.", status_code=400)
        if not isinstance(value, str):
            raise ValidationError(f"Object metadata value for {key!r} must be a string.", status_code=400)
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
        checked[key] = value
    if total > MAX_METADATA_BYTES:
        raise ValidationError(f"Object metadata is larger than {MAX_METADATA_BYTES} bytes.", status_code=400)
    return checked


class ObjectStore(ABC):
    """Stores uploaded files by key. See the module docstring for the contract."""

    @abstractmethod
    async def put_stream(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        """Store the bytes of ``stream`` under ``key``, replacing any object there.

        Returns the size and SHA-256, computed while the bytes are written. A
        caller bounding the size wraps ``stream`` and raises from it: the
        store then keeps nothing.
        """

    @abstractmethod
    def open_stream(
        self,
        key: str,
        byte_range: ByteRange | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """Read an object, or one range of it, as an async iterator of chunks.

        ``async with store.open_stream(key) as chunks: async for chunk in chunks``.
        The key is checked and the object looked up when the context is
        entered, so a missing object raises there.
        """

    @abstractmethod
    def local_copy(self, key: str, suffix: str | None = None) -> AbstractAsyncContextManager[Path]:
        """A path on this node holding the object, valid until the context exits.

        The path ends with ``suffix``, the key's own suffix by default, because
        the path-based parsers choose a reader from it. An adapter that can
        yield the stored file itself does so. Otherwise it downloads a copy and
        removes it on exit. Either way the caller only reads the path.
        """

    @abstractmethod
    async def stat(self, key: str) -> ObjectInfo:
        """Size, SHA-256 and metadata of an object."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Whether an object is stored under ``key``."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete an object. A missing one is not an error."""

    @abstractmethod
    async def delete_many(self, keys: Iterable[str]) -> None:
        """Delete several objects, missing ones included.

        Tries every key, even after a failure, then raises ``ObjectStoreError``
        naming the keys it could not delete. Every key is checked before
        anything is deleted.
        """

    @abstractmethod
    async def copy(self, src: str, dst: str) -> ObjectInfo:
        """Copy an object and its metadata to ``dst``, replacing any object there.

        The two objects are independent from then on: deleting one leaves the
        other.
        """

    @abstractmethod
    async def presign_get(self, key: str, ttl_seconds: int) -> str | None:
        """A URL that reads the object without credentials for ``ttl_seconds``.

        ``None`` when the store can't sign one (the filesystem), so the caller
        serves the bytes itself. ``ttl_seconds`` must be positive.
        """

    async def put_bytes(self, key: str, data: bytes, metadata: Mapping[str, str] | None = None) -> ObjectInfo:
        """Store ``data`` under ``key``: :meth:`put_stream` for content already in memory."""

        async def _one_chunk() -> AsyncIterator[bytes]:
            yield data

        return await self.put_stream(key, _one_chunk(), metadata)


__all__ = [
    "MAX_METADATA_BYTES",
    "RESERVED_METADATA_PREFIX",
    "ByteRange",
    "ObjectInfo",
    "ObjectStore",
    "validate_metadata",
]
