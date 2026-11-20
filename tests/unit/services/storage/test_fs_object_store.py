"""The filesystem ObjectStore: the conformance suite, then what only the filesystem does."""

import asyncio
import hashlib
import threading
from pathlib import Path

import pytest
from core.utils import object_keys as keys
from core.utils.exceptions import ObjectNotFoundError, ObjectStoreError
from object_store_conformance import ObjectStoreConformance, StreamFailed, failing, original, read_all
from services.storage import fs_object_store
from services.storage.fs_object_store import FsObjectStore


class TestFsObjectStore(ObjectStoreConformance):
    LOCAL_COPY_IS_STORED_FILE = True

    @pytest.fixture
    def store(self, tmp_path):
        return FsObjectStore(tmp_path / "data")


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(root) -> FsObjectStore:
    return FsObjectStore(root)


def stray_files(root: Path) -> list[str]:
    """Temporary files the store left behind."""
    return sorted(str(p.relative_to(root)) for p in root.rglob(".part-*"))


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


class Gate:
    """Holds a call in its thread until released, and records what it returned."""

    def __init__(self, func):
        self.func = func
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.result = None

    def __call__(self, *args):
        self.entered.set()
        self.release.wait(timeout=5)
        try:
            self.result = self.func(*args)
            return self.result
        finally:
            self.done.set()


def failing_replace(monkeypatch, fails):
    """Make ``os.replace`` raise for the first call ``fails(src, dst)`` picks."""
    real_replace = fs_object_store.os.replace
    failed = []

    def replace(src, dst):
        if not failed and fails(Path(src), Path(dst)):
            failed.append((src, dst))
            raise OSError(5, "Input/output error")
        return real_replace(src, dst)

    monkeypatch.setattr(fs_object_store.os, "replace", replace)
    return failed


# --- where objects land -------------------------------------------------------------


async def test_an_original_lands_under_its_key(store, root):
    key = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    await store.put_bytes(key, b"%PDF", {"filename": "report.pdf"})
    path = root.joinpath(*key.split("/"))
    assert path.read_bytes() == b"%PDF"
    sidecars = [p.name for p in path.parent.iterdir() if p != path]
    assert sidecars == [f".{path.name}.meta"]


async def test_upload_and_tmp_keys_land_under_their_prefix(store, root):
    upload = keys.upload_key(keys.new_upload_id(), ".docx")
    tmp = keys.tmp_key(".txt")
    await store.put_bytes(upload, b"u")
    await store.put_bytes(tmp, b"t")
    assert (root / upload).read_bytes() == b"u"
    assert (root / tmp).read_bytes() == b"t"


async def test_a_legacy_key_is_the_file_already_in_data_dir(store, root):
    root.mkdir()
    legacy = root / "1713700000000_a1b2_report.pdf"
    legacy.write_bytes(b"%PDF legacy")
    key = keys.legacy_key_for_source(str(legacy), str(root))
    assert key == "legacy/1713700000000_a1b2_report.pdf"
    assert await read_all(store, key) == b"%PDF legacy"
    info = await store.stat(key)
    # The store didn't write it: no SHA-256, no metadata.
    assert (info.size, info.sha256, dict(info.metadata)) == (11, None, {})
    async with store.local_copy(key) as path:
        assert path == legacy
    await store.delete(key)
    assert not legacy.exists()


async def test_local_copy_yields_the_stored_file_itself(store, root):
    key = original(".pdf")
    await store.put_bytes(key, b"%PDF")
    async with store.local_copy(key) as path:
        assert path == root.joinpath(*key.split("/"))
    assert path.exists()


async def test_a_copy_of_a_legacy_file_gets_a_sha256(store, root):
    root.mkdir()
    (root / "1713700000000_a1b2_report.pdf").write_bytes(b"%PDF old")
    copy = await store.copy("legacy/1713700000000_a1b2_report.pdf", original())
    assert copy.sha256 == hashlib.sha256(b"%PDF old").hexdigest()
    assert (await store.stat(copy.key)).sha256 == copy.sha256


async def test_the_root_is_created_on_the_first_write_only(root):
    store = FsObjectStore(root)
    assert not root.exists()
    assert not await store.exists(original())
    assert not root.exists()
    await store.put_bytes(original(), b"x")
    assert root.is_dir()


def test_a_relative_root_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert FsObjectStore("../data").root == (tmp_path.parent / "data").resolve()


# --- nothing left behind -------------------------------------------------------------


async def test_a_failed_put_leaves_no_temporary_file(store, root):
    key = original()
    with pytest.raises(StreamFailed):
        await store.put_stream(key, failing(b"partial", fail_after=3))
    assert stray_files(root) == []
    assert list(root.joinpath(*key.split("/")).parent.iterdir()) == []


async def test_a_cancelled_put_leaves_no_temporary_file(store, root):
    key = original()
    taken = asyncio.Event()

    async def hanging():
        yield b"first"
        taken.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store.put_stream(key, hanging()))
    await asyncio.wait_for(taken.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stray_files(root) == []


@pytest.mark.parametrize("call", ["put", "open_stream", "local_copy"])
async def test_a_call_cancelled_while_its_thread_acquires_releases_what_it_got(store, root, monkeypatch, call):
    key = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    await store.put_bytes(key, b"%PDF")

    if call == "put":
        gate = Gate(store._create_temp)
        monkeypatch.setattr(store, "_create_temp", gate)
        operation = store.put_bytes(original(), b"x")

        def released(created):
            return not created[0].exists() and created[1].closed

    elif call == "open_stream":
        gate = Gate(fs_object_store._open_with_size)
        monkeypatch.setattr(fs_object_store, "_open_with_size", gate)
        operation = read_all(store, key)

        def released(opened):
            return opened[0].closed

    else:
        gate = Gate(fs_object_store._copy_to_temp)
        monkeypatch.setattr(fs_object_store, "_copy_to_temp", gate)

        async def copy_under_another_suffix():
            async with store.local_copy(key, ".txt"):
                pass

        operation = copy_under_another_suffix()

        def released(copy):
            return not copy.exists()

    task = asyncio.create_task(operation)
    assert await asyncio.to_thread(gate.entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    assert await asyncio.to_thread(gate.done.wait, 5)
    for _ in range(250):
        if released(gate.result):
            break
        await asyncio.sleep(0.02)
    assert released(gate.result)


async def test_a_put_retries_when_its_directory_is_pruned_while_made(store, monkeypatch):
    real_mkdir = Path.mkdir
    pruned = []

    def mkdir(self, *args, **kwargs):
        if not pruned:
            # What a concurrent delete's rmdir of the parent looks like here.
            pruned.append(self)
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    key = original()
    await store.put_bytes(key, b"x")
    assert pruned and await read_all(store, key) == b"x"


async def test_a_first_put_whose_sidecar_cannot_be_renamed_leaves_nothing(store, root, monkeypatch):
    failed = failing_replace(monkeypatch, lambda src, dst: dst.name.endswith(".meta"))
    key = original()
    with pytest.raises(ObjectStoreError):
        await store.put_bytes(key, b"new", {"filename": "a.pdf"})
    assert failed
    assert not await store.exists(key)
    assert files_under(root) == []


async def test_delete_removes_the_sidecar_and_empty_directories(store, root):
    key = original()
    await store.put_bytes(key, b"x", {"filename": "a.pdf"})
    file_dir = root.joinpath(*key.split("/")).parent
    await store.delete(key)
    assert not file_dir.exists()
    assert not file_dir.parent.exists()
    assert (root / "originals").is_dir()


async def test_delete_keeps_a_directory_that_still_holds_an_object(store, root):
    first = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    second = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    await store.put_bytes(first, b"1")
    await store.put_bytes(second, b"2")
    await store.delete(first)
    assert await read_all(store, second) == b"2"


async def test_a_put_after_its_directory_was_pruned(store):
    key = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    other = keys.original_key("docs", "report", keys.new_upload_id(), ".pdf")
    await store.put_bytes(key, b"1")
    await store.delete(key)
    await store.put_bytes(other, b"2")
    assert await read_all(store, other) == b"2"


# --- the root is a boundary -------------------------------------------------------------


async def test_a_symlink_cannot_lead_a_key_out_of_the_root(store, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"secret")
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    key = "legacy/escape/secret.pdf"
    with pytest.raises(ObjectStoreError):
        await read_all(store, key)
    with pytest.raises(ObjectStoreError):
        await store.put_bytes("legacy/escape/new.pdf", b"x")
    with pytest.raises(ObjectStoreError):
        await store.delete(key)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.pdf"]


async def test_a_legacy_key_naming_a_directory_is_no_object(store, root):
    (root / "sub").mkdir(parents=True)
    assert not await store.exists("legacy/sub")
    with pytest.raises(ObjectNotFoundError):
        await store.stat("legacy/sub")
    with pytest.raises(ObjectNotFoundError):
        await read_all(store, "legacy/sub")
    await store.delete("legacy/sub")
    assert (root / "sub").is_dir()


# --- the metadata sidecar ------------------------------------------------------------------


async def test_an_unreadable_sidecar_reads_as_no_metadata(store, root):
    key = original()
    await store.put_bytes(key, b"x", {"filename": "a.pdf"})
    path = root.joinpath(*key.split("/"))
    path.with_name(f".{path.name}.meta").write_text("{not json")
    info = await store.stat(key)
    assert (info.size, info.sha256, dict(info.metadata)) == (1, None, {})


async def test_a_failed_overwrite_never_pairs_new_content_with_old_metadata(store, root, monkeypatch):
    key = original()
    await store.put_bytes(key, b"old", {"filename": "old.pdf"})
    failing_replace(monkeypatch, lambda src, dst: src.name.startswith(".part-") and dst.name.endswith(".meta"))
    with pytest.raises(ObjectStoreError):
        await store.put_bytes(key, b"new!", {"filename": "new.pdf"})
    info = await store.stat(key)
    assert (await read_all(store, key), info.sha256, dict(info.metadata)) == (b"new!", None, {})
    assert stray_files(root) == []


async def test_a_failed_content_rename_keeps_the_previous_object_whole(store, root, monkeypatch):
    key = original()
    await store.put_bytes(key, b"old", {"filename": "old.pdf"})
    path = root.joinpath(*key.split("/"))
    failing_replace(monkeypatch, lambda src, dst: dst.name == path.name)
    with pytest.raises(ObjectStoreError):
        await store.put_bytes(key, b"new!", {"filename": "new.pdf"})
    info = await store.stat(key)
    assert (await read_all(store, key), info.sha256, dict(info.metadata)) == (
        b"old",
        hashlib.sha256(b"old").hexdigest(),
        {"filename": "old.pdf"},
    )
    assert stray_files(root) == []


async def test_a_hashed_sidecar_is_never_another_objects_sidecar(store, root):
    long_name = "a" * 252
    hashed_name = hashlib.sha256(long_name.encode()).hexdigest()
    long_key, hashed_key = keys.legacy_key(long_name), keys.legacy_key(hashed_name)
    await store.put_bytes(long_key, b"long", {"filename": "long.pdf"})
    await store.put_bytes(hashed_key, b"hash", {"filename": "hash.pdf"})
    assert dict((await store.stat(long_key)).metadata) == {"filename": "long.pdf"}
    await store.delete(hashed_key)
    assert dict((await store.stat(long_key)).metadata) == {"filename": "long.pdf"}


async def test_a_long_legacy_name_gets_a_hashed_sidecar(store, root):
    name = "1713700000000_a1b2_" + "r" * 230 + ".pdf"
    assert len(name.encode()) <= 255 < len(f".{name}.meta".encode())
    key = keys.legacy_key(name)
    await store.put_bytes(key, b"long", {"filename": "long.pdf"})
    assert dict((await store.stat(key)).metadata) == {"filename": "long.pdf"}
    await store.delete(key)
    assert list(root.iterdir()) == []


# --- delete_many ------------------------------------------------------------------------------


async def test_delete_many_tries_every_key_and_names_the_failures(store, root, monkeypatch):
    kept, gone = original(), original()
    await store.put_bytes(kept, b"1")
    await store.put_bytes(gone, b"2")
    real_delete = FsObjectStore._delete

    def flaky(self, path, prefix):
        if path == root.joinpath(*kept.split("/")):
            raise PermissionError(13, "Permission denied")
        return real_delete(self, path, prefix)

    monkeypatch.setattr(FsObjectStore, "_delete", flaky)
    with pytest.raises(ObjectStoreError) as excinfo:
        await store.delete_many([kept, gone])
    assert excinfo.value.extra["keys"] == [kept]
    assert not await store.exists(gone)


# --- the event loop stays free -------------------------------------------------------------------


async def test_filesystem_calls_run_off_the_event_loop(store, monkeypatch):
    loop_thread = threading.get_ident()
    on_loop: list[bool] = []
    real_resolve = Path.resolve

    def recording_resolve(self, *args, **kwargs):
        on_loop.append(threading.get_ident() == loop_thread)
        return real_resolve(self, *args, **kwargs)

    key = original()
    await store.put_bytes(key, b"x")
    monkeypatch.setattr(Path, "resolve", recording_resolve)
    await store.stat(key)
    await read_all(store, key)
    await store.delete(key)
    assert on_loop and not any(on_loop)
