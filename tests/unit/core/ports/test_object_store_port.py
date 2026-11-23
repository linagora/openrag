"""The pure parts of the ObjectStore port: byte ranges, metadata rules, put_bytes."""

from collections.abc import AsyncIterable

import pytest
from core.ports import ObjectStore
from core.ports.object_store import (
    MAX_METADATA_BYTES,
    ByteRange,
    ObjectInfo,
    validate_metadata,
)
from core.utils.exceptions import InvalidByteRangeError, ValidationError

# --- ByteRange -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("byte_range", "size", "expected"),
    [
        (ByteRange(0), 10, (0, 10)),
        (ByteRange(0, 0), 10, (0, 1)),
        (ByteRange(3, 5), 10, (3, 3)),
        (ByteRange(9), 10, (9, 1)),
        (ByteRange(5, 100), 10, (5, 5)),
    ],
)
def test_byte_range_resolves_inclusive_and_clamps_the_end(byte_range, size, expected):
    assert byte_range.resolve(size) == expected


@pytest.mark.parametrize(("start", "size"), [(10, 10), (11, 10), (0, 0)])
def test_byte_range_starting_past_the_end_is_416(start, size):
    with pytest.raises(InvalidByteRangeError) as excinfo:
        ByteRange(start).resolve(size)
    assert excinfo.value.status_code == 416


@pytest.mark.parametrize(("start", "end"), [(-1, None), (5, 4)])
def test_malformed_byte_range_is_refused(start, end):
    with pytest.raises(InvalidByteRangeError):
        ByteRange(start, end)


# --- metadata --------------------------------------------------------------------


def test_metadata_is_copied():
    given = {"filename": "rapport été.pdf", "file-id": "x"}
    checked = validate_metadata(given)
    assert checked == given and checked is not given
    assert validate_metadata(None) == {}


@pytest.mark.parametrize("key", ["", "Filename", "file_name", "file id", "-x", "é", "x" * 65, 3])
def test_metadata_key_outside_what_s3_keeps_is_refused(key):
    with pytest.raises(ValidationError) as excinfo:
        validate_metadata({key: "v"})
    assert excinfo.value.status_code == 400


def test_metadata_reserved_prefix_is_refused():
    with pytest.raises(ValidationError):
        validate_metadata({"openrag-sha256": "x"})


def test_metadata_value_must_be_a_string():
    with pytest.raises(ValidationError):
        validate_metadata({"size": 3})


def test_metadata_value_with_a_lone_surrogate_is_refused():
    # A "\ud800" escape in a JSON body: a 400, not a UnicodeEncodeError.
    with pytest.raises(ValidationError):
        validate_metadata({"filename": "\ud800x.pdf"})


def test_metadata_size_is_counted_in_utf8_bytes():
    key = "k"
    fits = "x" * (MAX_METADATA_BYTES - len(key))
    validate_metadata({key: fits})
    with pytest.raises(ValidationError):
        validate_metadata({key: fits + "x"})
    # "é" is two bytes in UTF-8.
    with pytest.raises(ValidationError):
        validate_metadata({key: "é" * ((MAX_METADATA_BYTES - len(key)) // 2 + 1)})


# --- put_bytes -------------------------------------------------------------------


class _RecordingStore(ObjectStore):
    """Implements only put_stream; the rest is never called here."""

    def __init__(self):
        self.received: list[tuple[str, bytes, dict | None]] = []

    async def put_stream(self, key, stream: AsyncIterable[bytes], metadata=None):
        data = b"".join([chunk async for chunk in stream])
        self.received.append((key, data, metadata))
        return ObjectInfo(key=key, size=len(data), sha256="x")

    def open_stream(self, key, byte_range=None):
        raise NotImplementedError

    def local_copy(self, key, suffix=None):
        raise NotImplementedError

    async def stat(self, key):
        raise NotImplementedError

    async def exists(self, key):
        raise NotImplementedError

    async def delete(self, key):
        raise NotImplementedError

    async def delete_many(self, keys):
        raise NotImplementedError

    async def copy(self, src, dst):
        raise NotImplementedError

    async def presign_get(self, key, ttl_seconds):
        raise NotImplementedError


async def test_put_bytes_goes_through_put_stream():
    store = _RecordingStore()
    info = await store.put_bytes("tmp/k", b"hello", {"filename": "a.txt"})
    assert store.received == [("tmp/k", b"hello", {"filename": "a.txt"})]
    assert info.size == 5


def test_object_info_metadata_defaults_to_empty_and_read_only():
    info = ObjectInfo(key="tmp/k", size=0, sha256=None)
    assert dict(info.metadata) == {}
    with pytest.raises(TypeError):
        info.metadata["x"] = "y"  # type: ignore[index]
