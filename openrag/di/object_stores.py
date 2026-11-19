"""Factory for the :class:`ObjectStore` that holds uploaded files.

The filesystem store under ``paths.data_dir``, where uploads already live.
Construction does no I/O: the directory is created on the first write. Taking
the settings (not a path) keeps one place that picks the store, for the API
and for the indexing workers alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.storage.fs_object_store import FsObjectStore

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.object_store import ObjectStore


def create_upload_store(settings: Settings) -> ObjectStore:
    """Build the store of uploaded files from the root settings."""
    return FsObjectStore(settings.paths.data_dir)


__all__ = ["create_upload_store"]
