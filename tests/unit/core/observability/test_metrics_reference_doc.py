"""``metrics_reference.md`` is what dashboards and alert rules get written from.

A metric, label or label value that the doc gets wrong produces a query that
returns nothing — silently, the same failure mode as a metric that is never
exported. So the doc's tables and value lists are checked against the specs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.observability import metric_specs as specs

_DOC = Path(__file__).resolve().parents[4] / "docs/content/docs/documentation/metrics_reference.md"
_ROW = re.compile(r"^\| `(openrag_[a-z_]+)` \| (\w+) \| ([^|]+) \|")
_BULLET = re.compile(r"^- `(\w+)` — (.*)$")


def _doc_lines() -> list[str]:
    return _DOC.read_text().splitlines()


def _metric_rows() -> dict[str, tuple[str, set[str]]]:
    rows = {}
    for line in _doc_lines():
        match = _ROW.match(line)
        if match:
            name, kind, labels = match.groups()
            rows[name] = (kind, set(re.findall(r"`(\w+)`", labels)))
    return rows


def _label_values() -> dict[str, set[str]]:
    """The "Label values" bullets, with wrapped continuation lines joined."""
    bullets: dict[str, str] = {}
    current = None
    for line in _doc_lines():
        match = _BULLET.match(line)
        if match:
            current = match.group(1)
            bullets[current] = match.group(2)
        elif current is not None and line.startswith("  "):
            bullets[current] += " " + line.strip()
        else:
            current = None
    return {label: set(re.findall(r"`([\w-]+)`", text)) for label, text in bullets.items()}


def test_every_declared_metric_is_documented_and_nothing_else() -> None:
    assert set(_metric_rows()) == {spec.name for spec in specs.ALL_SPECS}


@pytest.mark.parametrize("spec", specs.ALL_SPECS, ids=lambda spec: spec.name)
def test_documented_kind_and_labels_match_the_spec(spec: specs.MetricSpec) -> None:
    kind, labels = _metric_rows()[spec.name]

    assert kind == spec.kind
    assert labels == set(spec.labels)


@pytest.mark.parametrize(
    ("label", "values"),
    [
        ("status", specs.INGEST_STATUS_VALUES),
        ("stage", specs.STAGE_VALUES),
        ("state", specs.INGEST_TASK_STATE_VALUES),
        ("operation", specs.INFERENCE_OPERATION_VALUES),
        ("outcome", specs.INFERENCE_OUTCOME_VALUES),
        ("kind", specs.TOKEN_KIND_VALUES),
    ],
)
def test_documented_label_values_match_the_spec(label: str, values: tuple[str, ...]) -> None:
    assert _label_values()[label] == set(values)


def test_every_parser_pool_is_documented() -> None:
    """``pool`` also takes document types, so the doc lists more than the enum."""
    assert set(specs.PARSER_POOL_VALUES) <= _label_values()["pool"]
