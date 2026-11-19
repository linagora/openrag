"""Finding uploaded originals no catalog row references (``scripts/find_orphan_originals.py``).

Files deleted before their originals went with them, a cleanup the process
stopped before it ended, and a failed job's ``.md`` copy with
``SAVE_UPLOADED_FILES=false`` all leave files under ``paths.data_dir`` that
no row references. :func:`scan_orphan_originals` lists them and, with
``delete``, removes them.

Only uploads are considered: regular files directly under ``data_dir`` whose
name carries the prefix every upload gets (``1713700000000_a1b2_report.pdf``),
``.md`` copies included. Anything else there, subdirectories included, is
never listed.

Three rules keep the delete safe:

* **Old enough.** An upload is on disk before its job writes its row, so a
  file younger than ``min_age_seconds`` is skipped.
* **The catalog must know this directory.** When no upload under
  ``data_dir``, young ones included, is referenced at all, the scan most
  likely runs with another ``DATA_DIR`` or database than the API, and nothing
  is deleted.
* **A failed check fails the scan** instead of reporting files as
  unreferenced, and :class:`OriginalsCleaner` checks each file again just
  before deleting it.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.storage.originals import DEFAULT_BATCH_SIZE, OriginalsCleaner

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository

# ``make_unique_filename``: a millisecond timestamp and four hex digits.
UPLOAD_NAME = re.compile(r"\d{13}_[0-9a-f]{4}_.")
MARKDOWN_SUFFIX = ".md"
# Below an hour, an upload still waiting in the queue could be taken for an orphan.
MIN_AGE_SECONDS_FLOOR = 3600.0


class OrphanScanRefused(RuntimeError):
    """``delete`` was asked where the catalog doesn't seem to know ``data_dir``."""


def validate_min_age(min_age_seconds: float) -> None:
    if not (math.isfinite(min_age_seconds) and min_age_seconds >= MIN_AGE_SECONDS_FLOOR):
        raise ValueError("The minimum age must be finite and at least one hour")


async def scan_orphan_originals(
    document_repo: DocumentRepository,
    data_dir: str | os.PathLike[str],
    *,
    min_age_seconds: float,
    delete: bool = False,
    now: float | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one ``orphan`` event per unreferenced upload, then a ``summary``.

    With ``delete``, the orphans and their ``.md`` copies are deleted once the
    scan is complete. Raises :class:`OrphanScanRefused` instead when no upload
    is referenced, and lets a failed catalog check propagate.
    """
    validate_min_age(min_age_seconds)
    root = Path(data_dir).resolve()
    cutoff = (time.time() if now is None else now) - min_age_seconds
    uploads, young = await asyncio.to_thread(_uploads_by_age, root, cutoff)

    orphans: list[str] = []
    orphan_bytes = 0
    referenced_count = 0
    for start in range(0, len(uploads), batch_size):
        batch = uploads[start : start + batch_size]
        sources = [str(path) for path, _ in batch]
        # A ``.md`` copy stays while the upload it was parsed from is referenced.
        parsed_from = [source.removesuffix(MARKDOWN_SUFFIX) for source in sources if source.endswith(MARKDOWN_SUFFIX)]
        referenced = await document_repo.referenced_sources([*sources, *parsed_from])
        for (_, stat), source in zip(batch, sources, strict=True):
            if source in referenced or (
                source.endswith(MARKDOWN_SUFFIX) and source.removesuffix(MARKDOWN_SUFFIX) in referenced
            ):
                referenced_count += 1
                continue
            orphans.append(source)
            orphan_bytes += stat.st_size
            yield {
                "type": "orphan",
                "path": source,
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }

    deleted: int | None = None
    if delete:
        if orphans and referenced_count == 0 and not await _any_referenced(document_repo, young, batch_size):
            raise OrphanScanRefused(
                f"No upload under {root} is referenced by the catalog, so DATA_DIR or the database "
                "probably differ from the API's. Run this where the API runs. Nothing was deleted."
            )
        await OriginalsCleaner(document_repo, root, batch_size=batch_size).delete_unreferenced(orphans)
        deleted = await asyncio.to_thread(lambda: sum(not Path(source).exists() for source in orphans))

    yield {
        "type": "summary",
        "data_dir": str(root),
        "uploads": len(uploads) + len(young),
        "young": len(young),
        "referenced": referenced_count,
        "orphans": len(orphans),
        "orphan_bytes": orphan_bytes,
        "deleted": deleted,
    }


def _uploads_by_age(root: Path, cutoff: float) -> tuple[list[tuple[Path, os.stat_result]], list[str]]:
    """The uploads directly under ``root`` modified before ``cutoff``, and the younger ones."""
    aged: list[tuple[Path, os.stat_result]] = []
    young: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not UPLOAD_NAME.match(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            if stat.st_mtime > cutoff:
                young.append(entry.path)
            else:
                aged.append((Path(entry.path), stat))
    aged.sort(key=lambda item: item[0])
    return aged, young


async def _any_referenced(document_repo: DocumentRepository, sources: list[str], batch_size: int) -> bool:
    for start in range(0, len(sources), batch_size):
        if await document_repo.referenced_sources(sources[start : start + batch_size]):
            return True
    return False


__all__ = ["MIN_AGE_SECONDS_FLOOR", "OrphanScanRefused", "scan_orphan_originals", "validate_min_age"]
