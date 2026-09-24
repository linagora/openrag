"""Synthetic canary configuration.

The canary indexes a small known document, retrieves it and deletes it on a
fixed cadence, so a broken ingest or retrieval path is noticed even while every
component reports itself healthy. See ``services/orchestrators/canary_service.py``.
"""

from __future__ import annotations

from pydantic import Field

from .base import ConfigMixin


class CanaryConfig(ConfigMixin):
    # Off by default: it writes a user, a partition and one document per run
    # into the deployment. Production turns it on (the Helm chart does).
    enabled: bool = False
    interval_seconds: int = Field(default=900, ge=60)
    # Lets the replica finish booting before the first run.
    initial_delay_seconds: int = Field(default=60, ge=0)
    # Budget for the indexing task to settle, queue wait included: the canary
    # document waits behind whatever real work is already queued.
    index_timeout_seconds: int = Field(default=600, ge=10)
    # Budget for the retrieval call and for the delete, each.
    request_timeout_seconds: int = Field(default=60, ge=1)
