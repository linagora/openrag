"""The emission guard — a build-time gate on metrics nothing produces.

The cardinality guard next door asks whether a metric is *safe to export*. This
one asks whether it is exported at all. A metric can be declared, given an
instrument and a recorder, covered by unit tests on that recorder and by an
end-to-end export test, and still never be written by production code — every
one of those tests passes against a metric that is absent from a real scrape.

That is not a cosmetic gap. An alert whose metric is never produced does not
error; it evaluates against an empty series and stays green forever, which is
indistinguishable from a healthy system. S3-4 has two `severity: critical` rules
in exactly that shape (`OpenRagIngestStalled` reads
`time() - max(openrag_ingest_last_parse_completion_timestamp_seconds)`;
`OpenRagCircuitBreakerOpen` reads `openrag_circuit_breaker_state == 1`).

Both of those recorders were, at one point, called from nowhere that ran — the
recorder had tests, the export path had tests, and deleting the production call
site left the whole suite green. This closes that hole for every future metric
instead of the two that were found by hand.

The chain each metric must complete, and which test covers which link::

    MetricSpec ──1──▶ instrument ──2──▶ recorder ──3──▶ production call site
                 └── test_every_spec_is_instrumented ──┘
                                       └── test_every_recorder_is_called_in_production

Static analysis rather than imports: reading the source cannot initialise Ray,
cannot need a live registry, and cannot be satisfied by a test that happens to
call the recorder itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core.observability
import pytest
from core.observability.metric_specs import ALL_SPECS

#: A recorder is a public function that writes a metric. The prefixes are the
#: convention the package already follows; ``get_metrics`` (serialises the
#: registry for a scrape) and ``report_once`` (error reporting) are not
#: recorders and are excluded by it.
_RECORDER_PREFIXES = ("record_", "observe_", "set_", "clear_")

_OBSERVABILITY_DIR = Path(core.observability.__file__).parent
_PACKAGE_ROOT = _OBSERVABILITY_DIR.parent.parent  # …/openrag


def _called_names(tree: ast.AST) -> set[str]:
    """Every name invoked in *tree*, whether ``f()`` or ``mod.f()``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _recorders() -> dict[str, set[str]]:
    """Recorder name → the names it calls, across the observability package."""
    found: dict[str, set[str]] = {}
    for path in _python_files(_OBSERVABILITY_DIR):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(_RECORDER_PREFIXES):
                found[node.name] = _called_names(node)
    return found


def _production_calls() -> set[str]:
    """Every name called from production code outside the observability package.

    Tests are excluded by construction — this walks the package, not the repo —
    so a recorder exercised only by its own unit test does not count as emitted.
    """
    calls: set[str] = set()
    for path in _python_files(_PACKAGE_ROOT):
        if _OBSERVABILITY_DIR in path.parents or path.parent == _OBSERVABILITY_DIR:
            continue
        calls |= _called_names(_parse(path))
    return calls


def _spec_variable_names() -> list[str]:
    """Module-level ``NAME = MetricSpec(...)`` assignments in metric_specs.py."""
    names: list[str] = []
    for node in _parse(_OBSERVABILITY_DIR / "metric_specs.py").body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if getattr(func, "id", None) == "MetricSpec":
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
    return names


def test_every_declared_spec_is_in_all_specs() -> None:
    """``ALL_SPECS`` is what the cardinality guard iterates, so a spec left out of
    it is exempt from the forbidden-label check — silently, and precisely for the
    metric someone added without reading the conventions.

    Found by AST rather than by importing the tuple: the question is whether the
    tuple matches what the module declares, which the tuple cannot answer itself.
    """
    declared = set(_spec_variable_names())
    registered = {spec.name for spec in ALL_SPECS}

    by_name = {}
    for node in _parse(_OBSERVABILITY_DIR / "metric_specs.py").body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if getattr(node.value.func, "id", None) == "MetricSpec":
                for kw in node.value.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                by_name[t.id] = kw.value.value

    missing = sorted(var for var in declared if by_name.get(var) not in registered)
    assert not missing, (
        f"spec(s) declared but absent from ALL_SPECS: {missing}. The cardinality "
        f"guard iterates ALL_SPECS, so these are exempt from the forbidden-label check."
    )


@pytest.mark.parametrize("spec_name", _spec_variable_names())
def test_every_spec_is_instrumented(spec_name: str) -> None:
    """Link 1: a spec that no module turns into an instrument is inert.

    It would still pass the cardinality guard — declaring safe labels on a metric
    that is never created is trivially safe — so nothing else catches it.
    """
    referenced = set()
    for path in _python_files(_OBSERVABILITY_DIR):
        if path.name == "metric_specs.py":
            continue
        referenced |= {n.id for n in ast.walk(_parse(path)) if isinstance(n, ast.Name)}

    assert spec_name in referenced, (
        f"{spec_name} is declared in metric_specs.py but no observability module builds "
        f"an instrument from it, so the metric can never be produced."
    )


def test_every_recorder_is_called_in_production() -> None:
    """Links 2-3: a recorder nothing calls means a metric nothing produces.

    Reachability is transitive: ``record_tokens`` is called only by
    ``record_usage_from_response``, which production calls. Requiring a *direct*
    external caller would fail that legitimately, so the check follows the call
    graph inside the package to a fixed point.
    """
    recorders = _recorders()
    external = _production_calls()

    reached = {name for name in recorders if name in external}
    changed = True
    while changed:
        changed = False
        for caller in list(reached):
            for callee in recorders[caller] & recorders.keys():
                if callee not in reached:
                    reached.add(callee)
                    changed = True

    orphaned = sorted(set(recorders) - reached)
    assert not orphaned, (
        f"recorder(s) never called from production code: {orphaned}. "
        f"The metric each writes cannot appear in a scrape, so any alert on it "
        f"evaluates against an empty series and stays green forever. Either call "
        f"it from the code path it measures, or delete it and its spec."
    )
