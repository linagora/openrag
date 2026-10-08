"""A mapping key given twice in a values file is not an error to YAML: the last
one silently wins. Two branches that each add a top-level block of the same
name merge without a conflict and leave the chart reading only one of them."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[3] / "infra" / "charts" / "openrag-stack"


class _DuplicateKeyLoader(yaml.SafeLoader):
    duplicates: list[str]


def _construct_mapping(loader: _DuplicateKeyLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set = set()
    for key_node, _ in node.value:
        # `<<:` is a merge key, not a key: it has no constructor of its own, so
        # constructing it crashed the check, and the keys it merges in may be
        # overridden explicitly, which is its whole point. SafeLoader's
        # construct_mapping below flattens it as usual.
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            loader.duplicates.append(f"{key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DuplicateKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _duplicates(text: str) -> list[str]:
    loader = _DuplicateKeyLoader(text)
    loader.duplicates = []
    try:
        loader.get_single_data()
    finally:
        loader.dispose()
    return loader.duplicates


@pytest.mark.parametrize("path", sorted(CHART_DIR.glob("values*.yaml")), ids=lambda p: p.name)
def test_values_file_has_no_duplicate_keys(path: Path):
    duplicates = _duplicates(path.read_text(encoding="utf-8"))
    assert duplicates == [], f"{path.name} repeats {duplicates}"


def test_a_merge_key_is_not_a_duplicate_and_does_not_crash_the_check():
    """A values file sharing a block through an anchor and `<<:` is valid YAML
    that Helm accepts; the check must not raise on it, nor count a merged key
    overridden explicitly as a repeat."""
    text = "base: &base\n  a: 1\n  b: 2\nderived:\n  <<: *base\n  b: 3\nother:\n  <<: *base\n"
    assert _duplicates(text) == []


def test_a_real_duplicate_beside_a_merge_key_is_still_caught():
    text = "base: &base\n  a: 1\nderived:\n  <<: *base\n  b: 1\n  b: 2\n"
    assert [d.split(" ")[0] for d in _duplicates(text)] == ["'b'"]
