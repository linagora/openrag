"""OriginalsCleaner: what it deletes, what it keeps, and that it never raises."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from services.storage.originals import OriginalsCleaner, markdown_sidecar_path, originals_cleaner_for


class FakeDocumentRepo:
    """Answers ``referenced_sources`` from a set, and records each call."""

    def __init__(self, referenced: set[str] | None = None, *, fail: bool = False) -> None:
        self.referenced = referenced or set()
        self.fail = fail
        self.calls: list[list[str]] = []

    async def referenced_sources(self, sources):
        batch = list(sources)
        self.calls.append(batch)
        if self.fail:
            raise ConnectionError("catalog down")
        return {source for source in batch if source in self.referenced}


@pytest.fixture
def data_dir(tmp_path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def upload(data_dir: Path, name: str, *, sidecar: bool = False) -> str:
    path = data_dir / name
    path.write_bytes(b"content")
    if sidecar:
        markdown_sidecar_path(path).write_text("# parsed")
    return str(path.resolve())


async def test_deletes_an_unreferenced_original_and_its_sidecar(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
    assert await cleaner.delete_unreferenced([source]) == 1
    assert list(data_dir.iterdir()) == []


async def test_keeps_an_original_a_copy_still_references(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    cleaner = OriginalsCleaner(FakeDocumentRepo(referenced={source}), data_dir)
    assert await cleaner.delete_unreferenced([source]) == 0
    assert Path(source).exists() and markdown_sidecar_path(source).exists()


async def test_keeps_a_sidecar_a_row_references_while_its_original_goes(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    sidecar = str(markdown_sidecar_path(source))
    cleaner = OriginalsCleaner(FakeDocumentRepo(referenced={sidecar}), data_dir)
    assert await cleaner.delete_unreferenced([source]) == 1
    assert not Path(source).exists() and Path(sidecar).exists()


async def test_exact_targets_leave_the_sidecar(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
    assert await cleaner.delete_unreferenced([source], with_sidecars=False) == 1
    assert not Path(source).exists() and markdown_sidecar_path(source).exists()


@pytest.mark.parametrize("with_sidecars", [True, False])
async def test_an_md_copy_stays_while_its_upload_is_referenced(data_dir, with_sidecars):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    sidecar = str(markdown_sidecar_path(source))
    cleaner = OriginalsCleaner(FakeDocumentRepo(referenced={source}), data_dir)
    assert await cleaner.delete_unreferenced([sidecar], with_sidecars=with_sidecars) == 0
    assert Path(sidecar).exists()


async def test_a_markdown_upload_and_its_sidecar_both_go(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_notes.md", sidecar=True)
    assert markdown_sidecar_path(source).name == "1713700000000_a1b2_notes.md.md"
    assert await OriginalsCleaner(FakeDocumentRepo(), data_dir).delete_unreferenced([source]) == 1
    assert list(data_dir.iterdir()) == []


async def test_a_leftover_sidecar_goes_even_when_the_original_is_gone(data_dir):
    source = upload(data_dir, "1713700000000_a1b2_report.pdf", sidecar=True)
    Path(source).unlink()
    assert await OriginalsCleaner(FakeDocumentRepo(), data_dir).delete_unreferenced([source]) == 0
    assert list(data_dir.iterdir()) == []


async def test_a_missing_original_is_not_an_error(data_dir):
    cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
    assert await cleaner.delete_unreferenced([str(data_dir / "gone.pdf")]) == 0


# --- the data directory is a boundary ------------------------------------------------


async def test_never_deletes_outside_the_data_directory(data_dir, tmp_path):
    outside = tmp_path / "index_url_download.pdf"
    outside.write_bytes(b"temp download")
    cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
    assert await cleaner.delete_unreferenced([str(outside), str(data_dir)]) == 0
    assert outside.exists() and data_dir.is_dir()


async def test_a_symlinked_directory_cannot_lead_out_of_the_data_directory(data_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"secret")
    (data_dir / "escape").symlink_to(outside, target_is_directory=True)
    cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
    assert await cleaner.delete_unreferenced([str(data_dir / "escape" / "secret.pdf")]) == 0
    assert (outside / "secret.pdf").exists()


async def test_a_relative_data_directory_is_resolved(data_dir, monkeypatch):
    monkeypatch.chdir(data_dir.parent)
    source = upload(data_dir, "1713700000000_a1b2_report.pdf")
    assert await OriginalsCleaner(FakeDocumentRepo(), "data").delete_unreferenced([source]) == 1


# --- best effort ------------------------------------------------------------------------


async def test_a_failed_reference_check_keeps_the_whole_batch(data_dir):
    sources = [upload(data_dir, f"{i}_report.pdf") for i in range(3)]
    cleaner = OriginalsCleaner(FakeDocumentRepo(fail=True), data_dir)
    assert await cleaner.delete_unreferenced(sources) == 0
    assert all(Path(source).exists() for source in sources)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_a_file_that_cannot_be_deleted_does_not_stop_the_others(data_dir):
    locked_dir = data_dir / "locked"
    locked_dir.mkdir()
    locked = locked_dir / "1_report.pdf"
    locked.write_bytes(b"x")
    free = upload(data_dir, "2_report.pdf")
    locked_dir.chmod(0o500)
    try:
        cleaner = OriginalsCleaner(FakeDocumentRepo(), data_dir)
        assert await cleaner.delete_unreferenced([str(locked.resolve()), free]) == 1
    finally:
        locked_dir.chmod(0o700)
    assert locked.exists() and not Path(free).exists()


# --- batches -------------------------------------------------------------------------------


async def test_checks_references_in_batches_without_repeats_or_blanks(data_dir):
    sources = [upload(data_dir, f"{i}_report.pdf") for i in range(5)]
    repo = FakeDocumentRepo(referenced={sources[1]})
    cleaner = OriginalsCleaner(repo, data_dir, batch_size=2)
    deleted = await cleaner.delete_unreferenced([*sources, sources[0], None, ""])
    assert deleted == 4
    # Each batch is checked with its sidecars, in one query.
    assert repo.calls == [[*batch, *(f"{s}.md" for s in batch)] for batch in (sources[0:2], sources[2:4], sources[4:5])]
    assert [Path(s).exists() for s in sources] == [False, True, False, False, False]


async def test_nothing_to_delete_asks_nothing(data_dir):
    repo = FakeDocumentRepo()
    assert await OriginalsCleaner(repo, data_dir).delete_unreferenced([None, ""]) == 0
    assert repo.calls == []


def test_batch_size_must_be_positive(data_dir):
    with pytest.raises(ValueError):
        OriginalsCleaner(FakeDocumentRepo(), data_dir, batch_size=0)


# --- source_of ------------------------------------------------------------------------------


class MetadataRepo(FakeDocumentRepo):
    def __init__(self, metadata=None, *, fail_read: bool = False):
        super().__init__()
        self.metadata = metadata
        self.fail_read = fail_read

    async def get_file_metadata(self, file_id, partition):
        if self.fail_read:
            raise ConnectionError("catalog down")
        return self.metadata


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        (MetadataRepo({"source": "/data/1_report.pdf", "filename": "report.pdf"}), "/data/1_report.pdf"),
        (MetadataRepo(None), None),
        (MetadataRepo({"filename": "report.pdf"}), None),
        (MetadataRepo({"source": ""}), None),
        (MetadataRepo({"source": 42}), None),
        (MetadataRepo(fail_read=True), None),
    ],
    ids=["source", "no-row", "no-source", "empty", "not-a-string", "read-fails"],
)
async def test_source_of(data_dir, repo, expected):
    assert await OriginalsCleaner(repo, data_dir).source_of("f1", "docs") == expected


# --- sources_in_partition ---------------------------------------------------------------


class ListingRepo(FakeDocumentRepo):
    def __init__(self, sources=(), *, fail_read: bool = False):
        super().__init__()
        self.sources = list(sources)
        self.fail_read = fail_read

    async def list_partition_sources(self, partition):
        if self.fail_read:
            raise ConnectionError("catalog down")
        return list(self.sources)


async def test_sources_in_partition(data_dir):
    repo = ListingRepo(["/data/1_a.pdf", "/data/2_b.pdf"])
    assert await OriginalsCleaner(repo, data_dir).sources_in_partition("docs") == ["/data/1_a.pdf", "/data/2_b.pdf"]


async def test_sources_in_partition_that_cannot_be_read_are_none(data_dir):
    assert await OriginalsCleaner(ListingRepo(fail_read=True), data_dir).sources_in_partition("docs") == []


async def test_sources_in_partition_on_a_connection_the_caller_holds(data_dir):
    repo = ListingRepo(fail_read=True)
    asked: list[str] = []

    async def on_held_connection(partition):
        asked.append(partition)
        return ["/data/1_a.pdf"]

    cleaner = OriginalsCleaner(repo, data_dir)
    assert await cleaner.sources_in_partition("docs", on_held_connection) == ["/data/1_a.pdf"]
    assert asked == ["docs"]


async def test_sources_on_a_held_connection_that_cannot_be_read_are_none(data_dir):
    async def failing(partition):
        raise ConnectionError("lock connection lost")

    assert await OriginalsCleaner(ListingRepo(), data_dir).sources_in_partition("docs", failing) == []


# --- originals_cleaner_for ----------------------------------------------------------------


@pytest.mark.parametrize("file_fences", ["ray", "postgres"])
def test_a_cleaner_only_with_postgres_fences(data_dir, file_fences):
    settings = SimpleNamespace(
        loader=SimpleNamespace(file_fences=file_fences), paths=SimpleNamespace(data_dir=data_dir)
    )
    repo = FakeDocumentRepo()
    cleaner = originals_cleaner_for(settings, repo)
    if file_fences == "ray":
        assert cleaner is None
    else:
        assert cleaner._data_dir == data_dir.resolve() and cleaner._document_repo is repo
