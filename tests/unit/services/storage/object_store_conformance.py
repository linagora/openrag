"""The ObjectStore contract as tests: every adapter must pass all of them.

Not collected on its own (the name doesn't start with ``test_``). An adapter's
test module subclasses :class:`ObjectStoreConformance` and provides the
``store`` fixture::

    class TestFsObjectStore(ObjectStoreConformance):
        LOCAL_COPY_IS_STORED_FILE = True

        @pytest.fixture
        def store(self, tmp_path):
            return FsObjectStore(tmp_path)

What only one adapter does (where a file lands, how a presigned URL reads)
is tested in that adapter's own module, not here.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest
from core.ports.object_store import ByteRange, ObjectInfo, ObjectStore
from core.utils import object_keys as keys
from core.utils.exceptions import (
    InvalidByteRangeError,
    InvalidObjectKeyError,
    ObjectNotFoundError,
    ValidationError,
)

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class StreamFailed(Exception):
    """Raised by a test stream to stand for a client that went away or a size cap."""


def original(suffix: str = ".pdf", file_id: str = "report") -> str:
    return keys.original_key("docs", file_id, keys.new_upload_id(), suffix)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def chunks(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    """``data`` in uneven chunks, yielding to the loop between them."""
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


async def failing(data: bytes, fail_after: int) -> AsyncIterator[bytes]:
    """Yield ``fail_after`` bytes of ``data``, then raise."""
    yield data[:fail_after]
    await asyncio.sleep(0)
    raise StreamFailed("stream broke")


async def read_all(store: ObjectStore, key: str, byte_range: ByteRange | None = None) -> bytes:
    async with store.open_stream(key, byte_range) as stream:
        return b"".join([chunk async for chunk in stream])


def invalid_keys() -> Iterable[str]:
    return [
        "",
        "../etc/passwd",
        "/etc/passwd",
        "unknown/x",
        "legacy/../x",
        "legacy/tmp/x",
        "legacy/.part-0123",
        "legacy/a/.b.pdf",
        "originals/a/b",
        "uploads/" + "0" * 31,
        "legacy/" + "x" * 1100,
    ]


class ObjectStoreConformance:
    """The tests every ObjectStore adapter passes."""

    # True when local_copy yields the stored file itself (the filesystem):
    # the path then outlives the context. False when it yields a download,
    # which must be gone once the context exits.
    LOCAL_COPY_IS_STORED_FILE: bool = False
    # Over S3's 5 MiB multipart part size, so the multipart path runs.
    LARGE_OBJECT_BYTES: int = 6 * 1024 * 1024 + 123
    # Over S3's 1,000 keys per DeleteObjects call.
    DELETE_MANY_COUNT: int = 1001

    @pytest.fixture
    def store(self) -> ObjectStore:
        raise NotImplementedError("An adapter's test class provides the store fixture.")

    # --- round trip ---------------------------------------------------------------

    @pytest.mark.parametrize(
        "make_key",
        [
            lambda: original(),
            lambda: keys.upload_key(keys.new_upload_id(), ".docx"),
            lambda: keys.tmp_key(".txt"),
            lambda: keys.tmp_key(),
            lambda: keys.legacy_key("1713700000000_a1b2_rapport été.pdf"),
            lambda: keys.markdown_sidecar_key(original()),
        ],
        ids=["original", "upload", "tmp", "tmp-no-suffix", "legacy", "sidecar"],
    )
    async def test_round_trip_for_every_kind_of_key(self, store, make_key):
        key = make_key()
        data = b"%PDF-1.7 hello object store"
        info = await store.put_stream(key, chunks(data))
        assert info == ObjectInfo(key=key, size=len(data), sha256=sha256(data))
        assert await read_all(store, key) == data
        assert await store.exists(key)

    async def test_large_object_round_trips_with_its_sha256(self, store):
        key = original()
        data = bytes(range(256)) * (self.LARGE_OBJECT_BYTES // 256) + b"tail"
        info = await store.put_stream(key, chunks(data, size=1024 * 1024 + 17))
        assert info.size == len(data)
        assert info.sha256 == sha256(data)
        assert await read_all(store, key) == data

    async def test_empty_object(self, store):
        key = keys.tmp_key(".txt")
        info = await store.put_bytes(key, b"")
        assert (info.size, info.sha256) == (0, EMPTY_SHA256)
        assert await read_all(store, key) == b""
        assert (await store.stat(key)).size == 0

    async def test_stat_matches_the_put(self, store):
        key = original()
        put = await store.put_bytes(key, b"abc", {"filename": "a.pdf"})
        assert await store.stat(key) == put

    async def test_put_replaces_content_and_metadata(self, store):
        key = original()
        await store.put_bytes(key, b"first version", {"filename": "v1.pdf", "rev": "1"})
        await store.put_bytes(key, b"second", {"filename": "v2.pdf"})
        assert await read_all(store, key) == b"second"
        info = await store.stat(key)
        assert dict(info.metadata) == {"filename": "v2.pdf"}
        assert info.sha256 == sha256(b"second")

    # --- metadata -----------------------------------------------------------------

    async def test_metadata_round_trips_exactly(self, store):
        key = original()
        metadata = {"filename": "rapport d'été — n°3.pdf", "file-id": "a:b.c", "empty": ""}
        put = await store.put_bytes(key, b"x", metadata)
        assert dict(put.metadata) == metadata
        assert dict((await store.stat(key)).metadata) == metadata

    async def test_no_metadata_is_empty_metadata(self, store):
        key = original()
        await store.put_bytes(key, b"x")
        assert dict((await store.stat(key)).metadata) == {}

    async def test_invalid_metadata_is_refused_before_writing(self, store):
        key = original()
        with pytest.raises(ValidationError):
            await store.put_bytes(key, b"x", {"Bad Key": "v"})
        assert not await store.exists(key)

    # --- failed writes --------------------------------------------------------------

    async def test_a_stream_that_raises_leaves_nothing(self, store):
        key = original()
        with pytest.raises(StreamFailed):
            await store.put_stream(key, failing(b"partial content", fail_after=7))
        assert not await store.exists(key)
        with pytest.raises(ObjectNotFoundError):
            await store.stat(key)

    async def test_a_stream_that_raises_keeps_the_previous_object(self, store):
        key = original()
        await store.put_bytes(key, b"previous", {"filename": "old.pdf"})
        with pytest.raises(StreamFailed):
            await store.put_stream(key, failing(b"replacement", fail_after=4), {"filename": "new.pdf"})
        assert await read_all(store, key) == b"previous"
        assert dict((await store.stat(key)).metadata) == {"filename": "old.pdf"}

    async def test_a_cancelled_put_leaves_nothing(self, store):
        key = original()
        first_chunk_taken = asyncio.Event()

        async def hanging() -> AsyncIterator[bytes]:
            yield b"first chunk"
            first_chunk_taken.set()
            await asyncio.Event().wait()
            yield b"never"

        task = asyncio.create_task(store.put_stream(key, hanging()))
        await asyncio.wait_for(first_chunk_taken.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await store.exists(key)

    # --- ranged reads ---------------------------------------------------------------

    @pytest.mark.parametrize(
        ("byte_range", "expected"),
        [
            (ByteRange(0, 0), b"0"),
            (ByteRange(3, 5), b"345"),
            (ByteRange(7), b"789"),
            (ByteRange(8, 100), b"89"),
            (ByteRange(0), b"0123456789"),
        ],
    )
    async def test_ranged_read(self, store, byte_range, expected):
        key = original()
        await store.put_bytes(key, b"0123456789")
        assert await read_all(store, key, byte_range) == expected

    async def test_ranged_read_of_a_large_object(self, store):
        key = original()
        data = bytes(range(256)) * 8192
        await store.put_bytes(key, data)
        start, end = 1024 * 1024 + 5, 2 * 1024 * 1024 + 9
        assert await read_all(store, key, ByteRange(start, end)) == data[start : end + 1]

    @pytest.mark.parametrize(("content", "start"), [(b"0123456789", 10), (b"0123456789", 50), (b"", 0)])
    async def test_range_past_the_end_is_refused_on_open(self, store, content, start):
        key = original()
        await store.put_bytes(key, content)
        with pytest.raises(InvalidByteRangeError):
            async with store.open_stream(key, ByteRange(start)):
                pass

    # --- missing objects --------------------------------------------------------------

    async def test_reading_a_missing_object_raises_not_found(self, store):
        key = original()
        assert not await store.exists(key)
        with pytest.raises(ObjectNotFoundError):
            await store.stat(key)
        with pytest.raises(ObjectNotFoundError):
            async with store.open_stream(key):
                pass
        with pytest.raises(ObjectNotFoundError):
            async with store.local_copy(key):
                pass
        with pytest.raises(ObjectNotFoundError):
            await store.copy(key, original())

    # --- delete ---------------------------------------------------------------------

    async def test_delete_is_idempotent(self, store):
        key = original()
        await store.put_bytes(key, b"x")
        await store.delete(key)
        assert not await store.exists(key)
        await store.delete(key)
        await store.delete(original())

    async def test_delete_leaves_neighbours(self, store):
        key = original()
        sidecar = keys.markdown_sidecar_key(key)
        sibling = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
        for k in (key, sidecar, sibling):
            await store.put_bytes(k, k.encode())
        await store.delete(key)
        assert await read_all(store, sidecar) == sidecar.encode()
        assert await read_all(store, sibling) == sibling.encode()

    async def test_a_key_can_be_written_again_after_delete(self, store):
        key = original()
        await store.put_bytes(key, b"one", {"filename": "a"})
        await store.delete(key)
        await store.put_bytes(key, b"two")
        assert await read_all(store, key) == b"two"
        assert dict((await store.stat(key)).metadata) == {}

    async def test_delete_many(self, store):
        present = [keys.tmp_key(".txt") for _ in range(self.DELETE_MANY_COUNT)]
        await asyncio.gather(*(store.put_bytes(k, b"x") for k in present))
        kept = original()
        await store.put_bytes(kept, b"kept")
        await store.delete_many([*present, original(), present[0]])
        assert not any(await asyncio.gather(*(store.exists(k) for k in present)))
        assert await store.exists(kept)
        await store.delete_many([])
        await store.delete_many(iter(present[:3]))

    async def test_delete_many_checks_every_key_first(self, store):
        key = original()
        await store.put_bytes(key, b"x")
        with pytest.raises(InvalidObjectKeyError):
            await store.delete_many([key, "../etc/passwd"])
        assert await store.exists(key)

    # --- copy -----------------------------------------------------------------------

    async def test_copy_carries_content_metadata_and_sha256(self, store):
        src, dst = original(), original(file_id="report-copy")
        data = b"copied content"
        await store.put_bytes(src, data, {"filename": "rapport été.pdf"})
        info = await store.copy(src, dst)
        assert info == ObjectInfo(
            key=dst, size=len(data), sha256=sha256(data), metadata={"filename": "rapport été.pdf"}
        )
        assert await read_all(store, dst) == data
        assert await store.stat(dst) == info

    async def test_copies_are_independent(self, store):
        src, dst = original(), original(file_id="report-copy")
        await store.put_bytes(src, b"shared")
        await store.copy(src, dst)
        await store.put_bytes(src, b"changed")
        assert await read_all(store, dst) == b"shared"
        await store.delete(src)
        assert await read_all(store, dst) == b"shared"

    async def test_copy_replaces_the_destination(self, store):
        src, dst = original(), original(file_id="report-copy")
        await store.put_bytes(src, b"new", {"filename": "new.pdf"})
        await store.put_bytes(dst, b"old destination", {"filename": "old.pdf", "rev": "1"})
        await store.copy(src, dst)
        assert await read_all(store, dst) == b"new"
        assert dict((await store.stat(dst)).metadata) == {"filename": "new.pdf"}

    # --- local_copy ---------------------------------------------------------------------

    async def test_local_copy_keeps_the_key_suffix(self, store):
        key = original(".pdf")
        await store.put_bytes(key, b"%PDF local")
        async with store.local_copy(key) as path:
            assert isinstance(path, Path)
            assert path.suffix == ".pdf"
            assert path.read_bytes() == b"%PDF local"
        self._check_local_copy_after_exit(path)
        assert await read_all(store, key) == b"%PDF local"

    async def test_local_copy_under_another_suffix(self, store):
        key = original(".pdf")
        await store.put_bytes(key, b"%PDF other suffix")
        async with store.local_copy(key, ".PDF") as path:
            assert path.name.endswith(".PDF")
            assert path.read_bytes() == b"%PDF other suffix"
        # A path made for another suffix is the adapter's own, whatever the adapter.
        assert not path.exists()
        assert await read_all(store, key) == b"%PDF other suffix"

    async def test_local_copy_of_a_legacy_key(self, store):
        key = keys.legacy_key("1713700000000_a1b2_report.docx")
        await store.put_bytes(key, b"PK legacy")
        async with store.local_copy(key) as path:
            assert path.suffix == ".docx"
            assert path.read_bytes() == b"PK legacy"

    async def test_local_copy_is_cleaned_up_when_the_body_raises(self, store):
        key = original(".pdf")
        await store.put_bytes(key, b"%PDF")
        with pytest.raises(StreamFailed):
            async with store.local_copy(key) as path:
                raise StreamFailed("parser crashed")
        self._check_local_copy_after_exit(path)
        assert await store.exists(key)

    @pytest.mark.parametrize("suffix", ["pdf", "../x", ".a/b", "./"])
    async def test_local_copy_refuses_a_suffix_that_could_leave_its_directory(self, store, suffix):
        key = original(".pdf")
        await store.put_bytes(key, b"%PDF")
        with pytest.raises(InvalidObjectKeyError):
            async with store.local_copy(key, suffix):
                pass

    def _check_local_copy_after_exit(self, path: Path) -> None:
        if self.LOCAL_COPY_IS_STORED_FILE:
            assert path.exists()
        else:
            assert not path.exists()

    # --- presign ----------------------------------------------------------------------

    async def test_presign_get_is_a_url_or_none(self, store):
        key = original()
        await store.put_bytes(key, b"x")
        url = await store.presign_get(key, 60)
        assert url is None or url.startswith(("http://", "https://"))

    @pytest.mark.parametrize("ttl", [0, -1])
    async def test_presign_get_refuses_a_ttl_that_is_not_positive(self, store, ttl):
        with pytest.raises(ValueError):
            await store.presign_get(original(), ttl)

    # --- invalid keys -------------------------------------------------------------------

    @pytest.mark.parametrize("key", invalid_keys())
    async def test_every_method_refuses_an_invalid_key(self, store, key):
        valid = original()
        await store.put_bytes(valid, b"x")
        calls = [
            lambda: store.put_bytes(key, b"x"),
            lambda: store.stat(key),
            lambda: store.exists(key),
            lambda: store.delete(key),
            lambda: store.delete_many([key]),
            lambda: store.copy(key, original()),
            lambda: store.copy(valid, key),
            lambda: store.presign_get(key, 60),
        ]
        for call in calls:
            with pytest.raises(InvalidObjectKeyError):
                await call()
        with pytest.raises(InvalidObjectKeyError):
            async with store.open_stream(key):
                pass
        with pytest.raises(InvalidObjectKeyError):
            async with store.local_copy(key):
                pass

    async def test_a_hostile_file_id_stays_its_own_object(self, store):
        hostile = keys.original_key("docs", "..", keys.new_upload_id(), ".pdf")
        neighbour = keys.original_key("docs", ".", keys.new_upload_id(), ".pdf")
        await store.put_bytes(hostile, b"dotdot")
        await store.put_bytes(neighbour, b"dot")
        assert await read_all(store, hostile) == b"dotdot"
        assert await read_all(store, neighbour) == b"dot"

    # --- concurrency ----------------------------------------------------------------------

    async def test_concurrent_puts_to_different_keys(self, store):
        payloads = {original(file_id=f"f{i}"): f"content {i}".encode() * 1000 for i in range(20)}
        infos = await asyncio.gather(*(store.put_stream(k, chunks(v, 1000)) for k, v in payloads.items()))
        assert [info.sha256 for info in infos] == [sha256(v) for v in payloads.values()]
        for key, data in payloads.items():
            assert await read_all(store, key) == data

    async def test_concurrent_puts_to_one_key_never_mix(self, store):
        key = original()
        versions = [bytes([65 + i]) * 50_000 for i in range(8)]
        await asyncio.gather(*(store.put_stream(key, chunks(v, 4096)) for v in versions))
        final = await read_all(store, key)
        assert final in versions
        assert (await store.stat(key)).sha256 == sha256(final)
