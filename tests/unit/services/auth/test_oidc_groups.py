"""Unit tests for :func:`services.auth.oidc_groups.extract_partition_roles`.

Pure string-transform tests — no DB, no IdP. Pins the strategy-doc mapping
(``/openrag/project-alpha/editor`` → ``("project-alpha", "editor")``) and the
edge cases that must never raise (malformed claim, wrong prefix, unknown role).
"""

from __future__ import annotations

from core.config.auth import OIDCConfig
from services.auth.oidc_groups import extract_partition_roles


def _cfg(**over) -> OIDCConfig:
    base = {
        "claim_groups": "groups",
        "group_prefix": "/openrag/",
        "group_pattern": r"(.+)/(owner|editor|viewer)$",
    }
    base.update(over)
    return OIDCConfig(**base)


def test_disabled_when_claim_groups_unset():
    cfg = _cfg(claim_groups="")
    assert extract_partition_roles({"groups": ["/openrag/alpha/owner"]}, cfg) == []


def test_basic_prefix_strip_and_pattern_match():
    cfg = _cfg()
    claims = {"groups": ["/openrag/project-alpha/editor"]}
    assert extract_partition_roles(claims, cfg) == [("project-alpha", "editor")]


def test_groups_without_prefix_are_ignored():
    """Groups belonging to other apps (no /openrag/ prefix) are skipped."""
    cfg = _cfg()
    claims = {"groups": ["/otherapp/x/editor", "noprefix", "/openrag/beta/owner"]}
    assert extract_partition_roles(claims, cfg) == [("beta", "owner")]


def test_non_matching_pattern_is_dropped():
    cfg = _cfg()
    # No role suffix → no match; unknown role → dropped.
    claims = {"groups": ["/openrag/alpha", "/openrag/alpha/superuser"]}
    assert extract_partition_roles(claims, cfg) == []


def test_highest_role_wins_per_partition():
    cfg = _cfg()
    claims = {
        "groups": [
            "/openrag/alpha/viewer",
            "/openrag/alpha/editor",
            "/openrag/alpha/owner",
        ]
    }
    assert extract_partition_roles(claims, cfg) == [("alpha", "owner")]


def test_result_is_sorted_and_deduped():
    cfg = _cfg()
    claims = {"groups": ["/openrag/zeta/viewer", "/openrag/alpha/editor"]}
    assert extract_partition_roles(claims, cfg) == [("alpha", "editor"), ("zeta", "viewer")]


def test_scalar_string_groups_claim_is_accepted():
    cfg = _cfg()
    assert extract_partition_roles({"groups": "/openrag/alpha/editor"}, cfg) == [("alpha", "editor")]


def test_missing_or_malformed_claim_returns_empty():
    cfg = _cfg()
    assert extract_partition_roles({}, cfg) == []
    assert extract_partition_roles({"groups": None}, cfg) == []
    assert extract_partition_roles({"groups": 42}, cfg) == []
    assert extract_partition_roles({"groups": {"a": 1}}, cfg) == []
    assert extract_partition_roles("not-a-dict", cfg) == []


def test_empty_prefix_matches_any_group():
    cfg = _cfg(group_prefix="")
    claims = {"groups": ["team/alpha/editor"]}
    # Greedy (.+) captures "team/alpha" as the partition.
    assert extract_partition_roles(claims, cfg) == [("team/alpha", "editor")]


def test_role_casing_is_normalised():
    cfg = _cfg(group_pattern=r"(.+)/(OWNER|EDITOR|VIEWER|owner|editor|viewer)$")
    assert extract_partition_roles({"groups": ["/openrag/alpha/EDITOR"]}, cfg) == [("alpha", "editor")]
