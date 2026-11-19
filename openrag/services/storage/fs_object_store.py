"""The filesystem ObjectStore: uploaded files under ``data_dir``.

A key maps to a path under the root, component by component:
``originals/{p}/{f}/{id}.pdf`` is ``{root}/originals/{p}/{f}/{id}.pdf``. A
``legacy/{path}`` key is the file already at ``{root}/{path}``, where uploads
were written before the object store, so nothing is copied. Every path is
resolved and must stay under the root, so a symlink can't lead a key out of it.

Next to each object, a hidden sidecar ``.{name}.meta`` holds its SHA-256 and
metadata. A file the store didn't write (a legacy one) has none: ``stat``
then gives no SHA-256 and empty metadata.

A write goes to a hidden temporary file in the target directory, renamed over
the object once the stream is read, so a reader sees an object whole or not at
all. The rename of the content and the rename of its sidecar are two steps:
a lock per key orders them against other writes and against ``stat`` in this
process. Two processes writing the same key at once could pair one's content
with the other's metadata; OpenRag writes each key once (one upload id per
revision), so that doesn't happen.

``local_copy`` yields the stored file itself when it has the suffix asked for,
which the keys make the usual case, and a temporary copy otherwise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, TypeVar

from core.ports.object_store import ByteRange, ObjectInfo, ObjectStore, validate_metadata
from core.utils import consts
from core.utils.exceptions import ObjectNotFoundError, ObjectStoreError
from core.utils.logging import get_logger
from core.utils.object_keys import LEGACY, ORIGINALS, parse_key, validate_suffix

logger = get_logger()

_T = TypeVar("_T")
_TEMP_PREFIX = ".part-"
_META_SUFFIX = ".meta"
_LOCK_STRIPES = 64
# Name-length limit of the filesystem, in bytes.
_MAX_NAME_BYTES = 255


class FsObjectStore(ObjectStore):
    """Objects as files under ``root`` (``paths.data_dir``)."""

    def __init__(self, root: str | os.PathLike[str], *, chunk_size: int = consts.FILE_READ_CHUNK_SIZE) -> None:
        # Resolved once, so every object path is compared against the real
        # directory. Created on the first write, not here.
        self._root = Path(root).resolve()
        self._chunk_size = chunk_size
        self._locks = [threading.Lock() for _ in range(_LOCK_STRIPES)]

    @property
    def root(self) -> Path:
        return self._root

    # --- writes -----------------------------------------------------------------

    async def put_stream(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        self._lexical_path(key)
        checked = validate_metadata(metadata)
        path = await self._path(key)
        temp, file = await self._io(key, self._create_temp, path.parent)
        hasher = hashlib.sha256()
        size = 0
        try:
            async for chunk in stream:
                await self._io(key, file.write, chunk)
                hasher.update(chunk)
                size += len(chunk)
            await self._io(key, file.close)
        except BaseException:
            await asyncio.to_thread(_discard, file, temp)
            raise
        digest = hasher.hexdigest()
        # The stream is read: from here the write completes, even if the
        # caller is cancelled while it runs.
        commit = asyncio.ensure_future(self._io(key, self._commit, temp, path, digest, checked))
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError:
            await commit
            raise
        return ObjectInfo(key=key, size=size, sha256=digest, metadata=MappingProxyType(checked))

    async def copy(self, src: str, dst: str) -> ObjectInfo:
        self._lexical_path(src)
        self._lexical_path(dst)
        src_path, dst_path = await self._path(src), await self._path(dst)
        size, digest, metadata = await self._io(src, self._copy, src, src_path, dst_path)
        return ObjectInfo(key=dst, size=size, sha256=digest, metadata=MappingProxyType(metadata))

    async def delete(self, key: str) -> None:
        path = await self._path(key)
        await self._io(key, self._delete, path, parse_key(key).prefix)

    async def delete_many(self, keys: Iterable[str]) -> None:
        keys = list(keys)
        for key in keys:
            # Every key is checked before anything is deleted.
            self._lexical_path(key)
        failed: list[str] = []
        for key in keys:
            try:
                path = await self._path(key)
                await self._io(key, self._delete, path, parse_key(key).prefix)
            except ObjectStoreError as exc:
                logger.bind(key=key, error=str(exc)).warning("Could not delete an object.")
                failed.append(key)
        if failed:
            raise ObjectStoreError(f"Could not delete {len(failed)} of {len(keys)} objects.", keys=failed)

    # --- reads ------------------------------------------------------------------

    @asynccontextmanager
    async def open_stream(self, key: str, byte_range: ByteRange | None = None) -> AsyncIterator[AsyncIterator[bytes]]:
        path = await self._path(key)
        file, size = await self._io(key, _open_with_size, key, path)
        try:
            offset, length = (0, size) if byte_range is None else byte_range.resolve(size)
            yield self._read_chunks(key, file, offset, length)
        finally:
            await asyncio.to_thread(file.close)

    @asynccontextmanager
    async def local_copy(self, key: str, suffix: str | None = None) -> AsyncIterator[Path]:
        wanted = self._lexical_path(key).suffix if suffix is None else validate_suffix(suffix)
        path = await self._path(key)
        if not await self._io(key, path.is_file):
            raise ObjectNotFoundError(f"No object under {key!r}.")
        if path.suffix == wanted:
            # The stored file itself. Not ours to remove.
            yield path
            return
        temp = await self._io(key, _copy_to_temp, key, path, wanted)
        try:
            yield temp
        finally:
            await asyncio.to_thread(temp.unlink, missing_ok=True)

    async def stat(self, key: str) -> ObjectInfo:
        path = await self._path(key)
        size, digest, metadata = await self._io(key, self._stat, key, path)
        return ObjectInfo(key=key, size=size, sha256=digest, metadata=MappingProxyType(metadata))

    async def exists(self, key: str) -> bool:
        path = await self._path(key)
        return await self._io(key, path.is_file)

    async def presign_get(self, key: str, ttl_seconds: int) -> str | None:
        self._lexical_path(key)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        # Nothing can sign a URL for a file on disk: the caller serves it.
        return None

    # --- helpers, on the event loop -----------------------------------------------

    def _lexical_path(self, key: str) -> Path:
        """The path ``key`` names, before any I/O. Raises ``InvalidObjectKeyError`` for a bad key."""
        parsed = parse_key(key)
        relative = parsed.relative_path if parsed.prefix == LEGACY else key
        return self._root.joinpath(*relative.split("/"))

    async def _path(self, key: str) -> Path:
        """The path of ``key``'s object, checked to stay under the root once symlinks are followed."""
        path = self._lexical_path(key)
        await self._io(key, self._confine, key, path)
        return path

    async def _io(self, key: str, func: Callable[..., _T], *args: Any) -> _T:
        """Run blocking filesystem work in a thread; an ``OSError`` becomes ``ObjectStoreError``."""
        try:
            return await asyncio.to_thread(func, *args)
        except OSError as exc:
            raise ObjectStoreError(f"Filesystem error on {key!r}: {exc}", key=key) from exc

    async def _read_chunks(self, key: str, file: IO[bytes], offset: int, length: int) -> AsyncIterator[bytes]:
        await self._io(key, file.seek, offset)
        remaining = length
        while remaining > 0:
            chunk = await self._io(key, file.read, min(self._chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    # --- helpers, in a thread -------------------------------------------------------

    def _confine(self, key: str, path: Path) -> None:
        resolved = path.resolve()
        if resolved == self._root or not resolved.is_relative_to(self._root):
            logger.bind(key=key).warning("An object path resolves outside the store's root.")
            raise ObjectStoreError(f"The path of {key!r} leaves the store's root.", key=key)

    def _lock(self, path: Path) -> threading.Lock:
        return self._locks[hash(path) % _LOCK_STRIPES]

    def _create_temp(self, directory: Path) -> tuple[Path, IO[bytes]]:
        # A delete may remove an empty directory between our mkdir and our
        # open: make it again and retry.
        for attempt in range(3):
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / f"{_TEMP_PREFIX}{uuid.uuid4().hex}"
            try:
                return temp, open(temp, "xb")  # noqa: SIM115 - closed by the caller
            except FileNotFoundError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def _write_meta_temp(self, directory: Path, digest: str | None, metadata: Mapping[str, str]) -> Path:
        temp, file = self._create_temp(directory)
        try:
            with file:
                file.write(json.dumps({"sha256": digest, "metadata": dict(metadata)}, ensure_ascii=False).encode())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp

    def _commit(self, temp: Path, path: Path, digest: str | None, metadata: Mapping[str, str]) -> None:
        try:
            meta_temp = self._write_meta_temp(path.parent, digest, metadata)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        try:
            with self._lock(path):
                os.replace(temp, path)
                os.replace(meta_temp, _meta_path(path))
        except BaseException:
            temp.unlink(missing_ok=True)
            meta_temp.unlink(missing_ok=True)
            raise

    def _stat(self, key: str, path: Path) -> tuple[int, str | None, dict[str, str]]:
        with self._lock(path):
            try:
                st = path.stat()
            except (FileNotFoundError, NotADirectoryError):
                raise ObjectNotFoundError(f"No object under {key!r}.") from None
            if not path.is_file():
                raise ObjectNotFoundError(f"No object under {key!r}.")
            digest, metadata = _read_meta(path)
        return st.st_size, digest, metadata

    def _copy(self, src: str, src_path: Path, dst_path: Path) -> tuple[int, str, dict[str, str]]:
        # Open the content and read its sidecar under one lock, so they
        # belong to the same write; the copy then reads the open file.
        with self._lock(src_path):
            source = _open_existing(src, src_path)
            _, metadata = _read_meta(src_path)
        with source:
            temp, file = self._create_temp(dst_path.parent)
            hasher = hashlib.sha256()
            size = 0
            try:
                with file:
                    while chunk := source.read(self._chunk_size):
                        file.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        # Hashed again rather than taken from the sidecar: right even for
        # a source the store didn't write.
        digest = hasher.hexdigest()
        self._commit(temp, dst_path, digest, metadata)
        return size, digest, metadata

    def _delete(self, path: Path, prefix: str) -> None:
        with self._lock(path):
            try:
                path.unlink(missing_ok=True)
            except IsADirectoryError:
                # A legacy key naming a directory names no object.
                return
            _meta_path(path).unlink(missing_ok=True)
        if prefix == ORIGINALS:
            # originals/{p}/{f}/: drop {f} then {p} once empty, never originals/.
            for directory in (path.parent, path.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    break


def _meta_path(path: Path) -> Path:
    name = f".{path.name}{_META_SUFFIX}"
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        # A long legacy name: the sidecar is named after its hash instead.
        name = f".{hashlib.sha256(path.name.encode('utf-8')).hexdigest()}{_META_SUFFIX}"
    return path.with_name(name)


def _read_meta(path: Path) -> tuple[str | None, dict[str, str]]:
    try:
        raw = json.loads(_meta_path(path).read_bytes())
        digest = raw["sha256"]
        metadata = raw["metadata"]
        if (digest is not None and not isinstance(digest, str)) or not isinstance(metadata, dict):
            raise ValueError("unexpected sidecar shape")
        return digest, {str(k): str(v) for k, v in metadata.items()}
    except FileNotFoundError:
        return None, {}
    except (ValueError, KeyError, TypeError) as exc:
        logger.bind(path=str(path), error=str(exc)).warning("Unreadable object metadata sidecar; ignoring it.")
        return None, {}


def _open_existing(key: str, path: Path) -> IO[bytes]:
    try:
        return open(path, "rb")  # noqa: SIM115 - closed by the caller
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise ObjectNotFoundError(f"No object under {key!r}.") from None


def _open_with_size(key: str, path: Path) -> tuple[IO[bytes], int]:
    file = _open_existing(key, path)
    try:
        return file, os.fstat(file.fileno()).st_size
    except BaseException:
        file.close()
        raise


def _copy_to_temp(key: str, path: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="openrag-object-")
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copyfile(path, temp)
    except FileNotFoundError:
        temp.unlink(missing_ok=True)
        raise ObjectNotFoundError(f"No object under {key!r}.") from None
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _discard(file: IO[bytes], temp: Path) -> None:
    try:
        file.close()
    finally:
        temp.unlink(missing_ok=True)


__all__ = ["FsObjectStore"]
