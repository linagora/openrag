"""scan_orphan_originals: which files under data_dir it lists, deletes, and never touches."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.storage.orphan_originals import JOB_AFTER_UPLOAD, OrphanScanRefused, scan_orphan_originals

DAY = 24 * 3600
NOW = time.time()


class FakeDocumentRepo:
    """Answers ``referenced_sources`` from a set, and records each call."""

    def __init__(self, referenced=(), *, fail: bool = False) -> None:
        self.referenced = set(referenced)
        self.fail = fail
        self.calls: list[list[str]] = []

    async def referenced_sources(self, sources):
        batch = list(sources)
        self.calls.append(batch)
        if self.fail:
            raise ConnectionError("catalog down")
        return {source for source in batch if source in self.referenced}


class FakeJobRepo:
    """Answers ``count_unsettled_jobs`` from the creation times of unsettled jobs."""

    def __init__(self, unsettled_created_at=()) -> None:
        self.unsettled_created_at = [datetime.fromtimestamp(t, UTC) for t in unsettled_created_at]
        self.calls: list[datetime] = []

    async def count_unsettled_jobs(self, *, created_before):
        self.calls.append(created_before)
        return sum(created < created_before for created in self.unsettled_created_at)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def put(data_dir: Path, name: str, *, age: float = 2 * DAY, content: bytes = b"content") -> str:
    path = data_dir / name
    path.write_bytes(content)
    os.utime(path, (NOW - age, NOW - age))
    return str(path.resolve())


async def scan(repo, data_dir, **kwargs):
    if kwargs.get("delete"):
        kwargs.setdefault("job_repo", FakeJobRepo())
    events = [event async for event in scan_orphan_originals(repo, data_dir, min_age_seconds=DAY, now=NOW, **kwargs)]
    return [e["path"] for e in events if e["type"] == "orphan"], events[-1]


async def test_lists_the_uploads_no_row_references_and_their_md_copies(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    kept_md = put(data_dir, "1713700000000_a1b2_kept.pdf.md")
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf", content=b"12345")
    orphan_md = put(data_dir, "1713700000001_c3d4_gone.pdf.md", content=b"# parsed")
    lone_md = put(data_dir, "1713700000002_e5f6_failed.pdf.md")

    orphans, summary = await scan(FakeDocumentRepo({kept}), data_dir)

    assert orphans == [orphan, orphan_md, lone_md]
    assert kept_md not in orphans
    assert summary == {
        "type": "summary",
        "data_dir": str(data_dir.resolve()),
        "uploads": 5,
        "young": 0,
        "referenced": 2,
        "orphans": 3,
        "orphan_bytes": 5 + 8 + 7,
        "deleted": None,
    }
    assert all(Path(path).exists() for path in orphans)


async def test_a_markdown_upload_and_its_md_copy_are_kept_while_referenced(data_dir):
    notes = put(data_dir, "1713700000000_a1b2_notes.md")
    put(data_dir, "1713700000000_a1b2_notes.md.md")
    orphans, summary = await scan(FakeDocumentRepo({notes}), data_dir)
    assert orphans == [] and summary["referenced"] == 2


async def test_skips_young_files_names_that_are_not_uploads_and_subdirectories(data_dir, tmp_path):
    put(data_dir, "1713700000000_a1b2_in_flight.pdf", age=3600)
    put(data_dir, "notes.txt")
    put(data_dir, "1713700000_a1b2_short_timestamp.pdf")
    (data_dir / "originals").mkdir()
    put(data_dir / "originals", "1713700000000_a1b2_nested.pdf")
    outside = tmp_path / "1713700000000_a1b2_outside.pdf"
    outside.write_bytes(b"x")
    (data_dir / "1713700000000_a1b2_link.pdf").symlink_to(outside)

    orphans, summary = await scan(FakeDocumentRepo(), data_dir)

    assert orphans == []
    assert summary["uploads"] == 1 and summary["young"] == 1


async def test_delete_removes_the_orphans_and_keeps_everything_else(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    kept_md = put(data_dir, "1713700000000_a1b2_kept.pdf.md")
    young = put(data_dir, "1713700000003_aaaa_young.pdf", age=3600)
    other = put(data_dir, "notes.txt")
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")
    orphan_md = put(data_dir, "1713700000001_c3d4_gone.pdf.md")

    orphans, summary = await scan(FakeDocumentRepo({kept}), data_dir, delete=True)

    assert orphans == [orphan, orphan_md]
    assert summary["deleted"] == 2
    assert not Path(orphan).exists() and not Path(orphan_md).exists()
    assert all(Path(path).exists() for path in (kept, kept_md, young, other))


async def test_delete_refuses_when_no_upload_is_referenced(data_dir):
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")
    events = []
    with pytest.raises(OrphanScanRefused, match="Nothing was deleted"):
        async for event in scan_orphan_originals(
            FakeDocumentRepo(), data_dir, min_age_seconds=DAY, now=NOW, delete=True, job_repo=FakeJobRepo()
        ):
            events.append(event)
    assert [e["type"] for e in events] == ["orphan"]
    assert Path(orphan).exists()


async def test_delete_goes_ahead_when_only_young_uploads_are_referenced(data_dir):
    # A deployment whose live files were all uploaded today still knows this directory.
    today = put(data_dir, "1713700000000_a1b2_today.pdf", age=3600)
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")

    orphans, summary = await scan(FakeDocumentRepo({today}), data_dir, delete=True)

    assert orphans == [orphan] and summary["deleted"] == 1 and summary["referenced"] == 0
    assert Path(today).exists() and not Path(orphan).exists()


async def test_delete_spares_a_young_md_copy_of_an_old_orphan(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")
    young_md = put(data_dir, "1713700000001_c3d4_gone.pdf.md", age=3600)

    orphans, summary = await scan(FakeDocumentRepo({kept}), data_dir, delete=True)

    assert orphans == [orphan] and summary["deleted"] == 1
    assert not Path(orphan).exists() and Path(young_md).exists()


async def test_delete_keeps_an_md_copy_whose_upload_became_referenced(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    late = put(data_dir, "1713700000001_c3d4_late.pdf")
    late_md = put(data_dir, "1713700000001_c3d4_late.pdf.md")

    class JobWritesItsRowMeanwhile(FakeDocumentRepo):
        async def referenced_sources(self, sources):
            answer = await super().referenced_sources(sources)
            self.referenced.add(late)
            return answer

    orphans, summary = await scan(JobWritesItsRowMeanwhile({kept}), data_dir, delete=True)

    assert orphans == [late, late_md] and summary["deleted"] == 0
    assert Path(late).exists() and Path(late_md).exists()


@pytest.mark.parametrize(
    "created_at",
    [NOW - 2 * DAY, NOW - DAY + 60],
    ids=["before-the-cutoff", "just-after-the-cutoff"],
)
async def test_delete_refuses_while_a_job_old_enough_to_own_an_orphan_runs(data_dir, created_at):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    orphan = put(data_dir, "1713700000001_c3d4_queued.pdf")
    with pytest.raises(OrphanScanRefused, match="haven't settled"):
        await scan(FakeDocumentRepo({kept}), data_dir, delete=True, job_repo=FakeJobRepo([created_at]))
    assert Path(orphan).exists()


async def test_delete_goes_ahead_while_only_recent_jobs_run(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")
    jobs = FakeJobRepo([NOW - 3600])

    orphans, summary = await scan(FakeDocumentRepo({kept}), data_dir, delete=True, job_repo=jobs)

    assert orphans == [orphan] and summary["deleted"] == 1
    assert jobs.calls == [datetime.fromtimestamp(NOW - DAY, UTC) + JOB_AFTER_UPLOAD]


async def test_delete_needs_the_job_repository(data_dir):
    with pytest.raises(ValueError, match="job repository"):
        _ = [e async for e in scan_orphan_originals(FakeDocumentRepo(), data_dir, min_age_seconds=DAY, delete=True)]


async def test_a_failed_check_fails_the_scan_instead_of_reporting_orphans(data_dir):
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")
    with pytest.raises(ConnectionError):
        await scan(FakeDocumentRepo(fail=True), data_dir, delete=True)
    assert Path(orphan).exists()


async def test_a_failed_check_just_before_the_delete_fails_the_scan(data_dir):
    kept = put(data_dir, "1713700000000_a1b2_kept.pdf")
    orphan = put(data_dir, "1713700000001_c3d4_gone.pdf")

    class DownBeforeTheDelete(FakeDocumentRepo):
        async def referenced_sources(self, sources):
            self.fail = bool(self.calls)
            return await super().referenced_sources(sources)

    with pytest.raises(ConnectionError):
        await scan(DownBeforeTheDelete({kept}), data_dir, delete=True)
    assert Path(orphan).exists()


async def test_checks_references_in_batches(data_dir):
    sources = [put(data_dir, f"171370000000{i}_a1b2_{i}.pdf") for i in range(5)]
    repo = FakeDocumentRepo({sources[0]})
    orphans, _ = await scan(repo, data_dir, batch_size=2)
    assert orphans == sources[1:]
    assert [len(call) for call in repo.calls] == [2, 2, 1]


async def test_nothing_under_data_dir_is_a_clean_scan(data_dir):
    orphans, summary = await scan(FakeDocumentRepo(), data_dir, delete=True)
    assert orphans == [] and summary["uploads"] == 0 and summary["deleted"] == 0


@pytest.mark.parametrize("min_age_seconds", [0, 3599, float("nan"), float("inf")])
async def test_the_minimum_age_is_at_least_an_hour(data_dir, min_age_seconds):
    with pytest.raises(ValueError, match="at least one hour"):
        _ = [e async for e in scan_orphan_originals(FakeDocumentRepo(), data_dir, min_age_seconds=min_age_seconds)]
