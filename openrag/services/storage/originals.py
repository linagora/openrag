"""Deleting uploaded originals once the catalog no longer references them.

An upload is kept under ``paths.data_dir`` as the file its catalog row's
``source`` names, and with ``SAVE_MARKDOWN`` a ``<source>.md`` copy of its
parse sits next to it. Deleting a file, a workspace's orphaned files or a
partition, replacing a file, and a failed job all leave such an original
behind; :class:`OriginalsCleaner` removes it.

Two rules make that safe:

* **Only what no row references.** A copy of a file keeps its source's
  ``source``, possibly in another partition, so a path is deleted only when
  :meth:`DocumentRepository.referenced_sources` no longer names it, checked
  just before the delete.
* **Only under ``data_dir``.** The path is resolved, symlinks included, and
  must stay inside it. Anything else (an MCP ``index_url`` download in the
  node's temp directory, a row from an older ``DATA_DIR``) is left alone.

Deleting is best effort: it runs after the user's operation has committed, so
a failure is logged and the file stays, for the orphan script to find later.
Nothing here raises.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository

logger = get_logger()

# Paths checked against the catalog in one query, and deleted in one thread hop.
DEFAULT_BATCH_SIZE = 500


def markdown_sidecar_path(source_path: str | os.PathLike[str]) -> Path:
    """Path of the ``SAVE_MARKDOWN`` copy for an upload stored at ``source_path``.

    The suffix is appended rather than swapped so a markdown upload is never
    overwritten.
    """
    return Path(f"{source_path}.md")


class OriginalsCleaner:
    """Deletes uploaded originals, with their markdown sidecar, once nothing references them."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        data_dir: str | os.PathLike[str],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._document_repo = document_repo
        # Resolved once: every path to delete is compared against the real directory.
        self._data_dir = Path(data_dir).resolve()
        self._batch_size = batch_size

    async def delete_unreferenced(self, sources: Iterable[str | None]) -> int:
        """Delete each of ``sources`` that no catalog row references. Returns how many originals went.

        Empty and repeated paths are skipped. A batch whose reference check
        fails is left whole: when in doubt, keep the file.
        """
        candidates = list(dict.fromkeys(source for source in sources if source))
        deleted = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            try:
                referenced = await self._document_repo.referenced_sources(batch)
            except Exception as exc:  # noqa: BLE001 - best effort, after the user's operation committed
                logger.bind(count=len(batch), error=str(exc)).warning(
                    "Could not check whether uploaded originals are still referenced; keeping them."
                )
                continue
            unreferenced = [source for source in batch if source not in referenced]
            if len(unreferenced) < len(batch):
                logger.bind(count=len(batch) - len(unreferenced)).debug(
                    "Kept uploaded originals that another catalog row still references."
                )
            if unreferenced:
                deleted += await asyncio.to_thread(self._delete_files, unreferenced)
        return deleted

    def _delete_files(self, sources: list[str]) -> int:
        deleted = 0
        for source in sources:
            path = Path(source)
            if not self._is_under_data_dir(path):
                logger.bind(source=source).debug("Not deleting an original outside the data directory.")
                continue
            for target in (path, markdown_sidecar_path(path)):
                try:
                    if target.is_file():
                        target.unlink()
                        if target == path:
                            deleted += 1
                except OSError as exc:
                    logger.bind(source=source, path=str(target), error=str(exc)).warning(
                        "Could not delete an uploaded original; it stays until the orphan script removes it."
                    )
        if deleted:
            logger.bind(count=deleted).info("Deleted uploaded originals no catalog row references.")
        return deleted

    def _is_under_data_dir(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved != self._data_dir and resolved.is_relative_to(self._data_dir)


__all__ = ["DEFAULT_BATCH_SIZE", "OriginalsCleaner", "markdown_sidecar_path"]
