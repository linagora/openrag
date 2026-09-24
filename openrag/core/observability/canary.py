"""Synthetic canary identity and run outcome.

Shared by the runner (``services/orchestrators/canary_service.py``), the
partition service that reserves the canary's partition name, and the metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Reserved: users cannot create a partition under this name, so nothing but
# the canary ever writes into it.
CANARY_PARTITION = "openrag-canary"

# The canary's system user is found by this address. ``.invalid`` is reserved
# (RFC 2606), so no identity provider can assert it and OIDC claim mapping can
# never attach it to a person. The user holds no API token: it cannot sign in.
CANARY_USER_EMAIL = "canary@openrag.invalid"
CANARY_USER_DISPLAY_NAME = "OpenRag canary"

# Canary file ids are ``canary-<unix seconds>-<nonce>``; the timestamp lets a
# later run tell a leftover from a crashed run apart from one still in flight.
CANARY_FILE_ID_PREFIX = "canary-"


def is_canary_partition(partition: str | None) -> bool:
    """Whether *partition* is the canary's, for callers that must exclude its traffic."""
    return partition == CANARY_PARTITION


class CanaryStage(str, Enum):
    """Where a canary run spent its time, or where it failed.

    Doubles as a metric label value, so the set is fixed.
    """

    SETUP = "setup"
    # The indexing task waiting for a worker, behind whatever real work is queued.
    QUEUE = "queue"
    # The indexing task running: parse, chunk, embed, insert.
    INDEX = "index"
    QUERY = "query"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class CanaryRunResult:
    passed: bool
    # First stage that failed. A cleanup failure after a failed query still
    # reports the query: that is the one that says what is broken.
    failed_stage: CanaryStage | None = None
    reason: str | None = None
    # Seconds spent in each stage the run reached.
    durations: dict[CanaryStage, float] = field(default_factory=dict)
    total_seconds: float = 0.0
