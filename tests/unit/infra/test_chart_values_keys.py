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
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            loader.duplicates.append(f"{key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DuplicateKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@pytest.mark.parametrize("path", sorted(CHART_DIR.glob("values*.yaml")), ids=lambda p: p.name)
def test_values_file_has_no_duplicate_keys(path: Path):
    loader = _DuplicateKeyLoader(path.read_text(encoding="utf-8"))
    loader.duplicates = []
    try:
        loader.get_single_data()
    finally:
        loader.dispose()
    assert loader.duplicates == [], f"{path.name} repeats {loader.duplicates}"
