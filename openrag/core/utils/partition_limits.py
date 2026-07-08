"""Shared partition quota helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from core.utils.exceptions import ConfigError


def max_partitions_for_user(user: Mapping[str, Any] | None) -> int | None:
    """Return the owned-partition cap for a caller.

    Admin callers are exempt. Negative values disable the cap; zero means a
    real limit of zero.
    """
    if user and user.get("is_admin", False):
        return None
    raw_limit = os.environ.get("MAX_PARTITIONS_PER_USER", "100")
    try:
        return int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ConfigError("MAX_PARTITIONS_PER_USER must be an integer.") from exc
