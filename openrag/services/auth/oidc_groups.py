"""Map IdP group claims to OpenRAG (partition, role) pairs (Phase 15).

Pure, I/O-free translation of a user's group memberships — as advertised in
the OIDC ID token — into the partition memberships OpenRAG understands. The
:class:`AuthService` membership-sync step calls :func:`extract_partition_roles`
and reconciles the result against the database; everything here is a string
transform so it is exhaustively unit-testable without a DB or an IdP.

The strategy doc (``REFACTORING_STRATEGY_v1.md`` §15C) specified this mapping:

    /openrag/project-alpha/editor  ->  ("project-alpha", "editor")

i.e. strip ``group_prefix``, then match ``group_pattern`` to capture the
partition (group 1) and role (group 2). ``is_admin`` is deliberately *not*
derivable here — that boundary lives in :class:`AuthService`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.config.auth import OIDCConfig

# Role precedence used to collapse duplicate grants for the same partition
# (e.g. groups granting both viewer and editor → editor wins). Kept local so
# this pure mapper carries no dependency on the orchestrator layer; the values
# mirror ``AuthService.ROLE_HIERARCHY`` (the canonical source).
_ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}


def _coerce_groups(raw: Any) -> list[str]:
    """Normalise the groups claim to a list of strings.

    IdPs vary: most emit a JSON array, some a single string. Anything else
    (number, dict, ``None``) yields no groups rather than raising — a
    malformed claim must never break the login.
    """
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [g for g in raw if isinstance(g, str)]
    return []


def extract_partition_roles(claims: Any, cfg: OIDCConfig) -> list[tuple[str, str]]:
    """Return ``[(partition, role), ...]`` parsed from the group claim.

    Returns an empty list when the feature is off (``claim_groups`` unset),
    the claim is missing/malformed, or no group matches the pattern. Groups
    not carrying ``group_prefix`` are ignored (they belong to other apps).
    When several groups grant the same partition, the highest role wins. The
    result is sorted for deterministic logging/testing.
    """
    claim_name = (cfg.claim_groups or "").strip()
    if not claim_name:
        return []
    groups = _coerce_groups(claims.get(claim_name) if isinstance(claims, dict) else None)
    if not groups:
        return []

    pattern = re.compile(cfg.group_pattern)
    prefix = cfg.group_prefix or ""

    best: dict[str, str] = {}
    for group in groups:
        candidate = group.strip()
        if prefix:
            if not candidate.startswith(prefix):
                continue
            candidate = candidate[len(prefix) :]
        match = pattern.match(candidate)
        if not match:
            continue
        partition = match.group(1).strip().strip("/")
        role = match.group(2).strip().lower()
        if not partition or role not in _ROLE_RANK:
            continue
        if partition not in best or _ROLE_RANK[role] > _ROLE_RANK[best[partition]]:
            best[partition] = role

    return sorted(best.items())


__all__ = ["extract_partition_roles"]
