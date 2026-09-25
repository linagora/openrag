"""The bundled Grafana dashboards must work in any Grafana, not only the Compose one.

The same JSON is provisioned from disk by the Compose overlay and shipped as
ConfigMaps by the chart, for a platform's own Grafana or the bundled one on
Kubernetes. Nothing in it may therefore assume the Compose stack's names: a data
source UID or a scrape job name that only Compose defines leaves every panel
blank anywhere else, with no error.

The query rules below carry the metric design into the dashboards: no unbounded
label, counters rated before they are aggregated, and no metric that nothing in
``openrag/`` declares, since an unknown name renders as an empty panel rather than
as an error.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DASHBOARDS = REPO / "infra/charts/openrag-stack/dashboards"

DS_VARIABLE = "DS_PROMETHEUS"
DS_REF = {"type": "prometheus", "uid": "${" + DS_VARIABLE + "}"}
# The built-in "Annotations & Alerts" query, which every dashboard carries.
GRAFANA_BUILTIN = {"type": "grafana", "uid": "-- Grafana --"}

# The Admin UI's System > Metrics link (GRAFANA_URL) deep-links this UID.
LINKED_UIDS = {"openrag-http"}

# Labels bounded by user or request behaviour rather than by configuration. The
# metrics never declare them; a query that groups or filters by one is written
# against a series that must not exist.
FORBIDDEN_LABELS = ("partition", "user_id", "file_id", "task_id", "request_id", "filename")

METRIC_NAME = re.compile(r"(?<![A-Za-z0-9_])(?:ray_)?(openrag_[a-z0-9_]+)")
DECLARED_NAME = re.compile(r"""["'](openrag_[a-z0-9_]+)["']""")
HISTOGRAM_SERIES = re.compile(r"_(?:bucket|sum|count)$")
UP = re.compile(r"\bup\b")
UP_ON_API_JOB = re.compile(r'\bup\s*\{[^}]*\bjob\s*=~\s*"\$job"[^}]*\}')

# The series the job variable reads. It must exist before the API serves a
# request: the HTTP counters are labelled, so they export nothing until the
# first request, and the middleware skips /metrics and the probes, so neither
# scrapes nor health checks create one. This gauge is unlabelled and set when
# the monitoring module loads, and as a prometheus_client metric it is served
# only by the API's /metrics, never by Ray's exporter.
API_JOB_METRIC = "openrag_model_endpoint_discovery_up"

# Grafana's "All" selection, which the hidden job variable keeps.
ALL_VALUE = "$__all"

# Scrapes /metrics twice through the instrumentation middleware, as Prometheus
# does on an API nobody uses yet, and prints the second scrape.
IDLE_API_SCRAPE = """
from api.middleware.instrumentation import InstrumentationMiddleware
from api.routers.admin.monitoring import get_metrics_access, router
from core.config.infrastructure import ServerConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.add_middleware(InstrumentationMiddleware)
app.include_router(router)
app.dependency_overrides[get_metrics_access] = lambda: ServerConfig(metrics_allow_unauthenticated=True)
with TestClient(app) as client:
    client.get("/metrics").raise_for_status()
    response = client.get("/metrics")
    response.raise_for_status()
print(response.text)
"""


def _load() -> dict[str, dict]:
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(DASHBOARDS.glob("*.json"))}


DASHBOARD_FILES = _load()


def _panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels", []))


def _walk(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _queries() -> list[tuple[str, str]]:
    """Every PromQL string a dashboard sends: panel targets and variable queries."""
    found = []
    for name, dashboard in DASHBOARD_FILES.items():
        for panel in _panels(dashboard.get("panels", [])):
            for target in panel.get("targets", []):
                if target.get("expr"):
                    found.append((f"{name}: {panel.get('title')} [{target.get('refId')}]", target["expr"]))
        for variable in dashboard.get("templating", {}).get("list", []):
            query = variable.get("query")
            if isinstance(query, dict):
                query = query.get("query")
            if variable.get("type") == "query" and query:
                found.append((f"{name}: variable {variable['name']}", query))
    return found


QUERIES = _queries()


@cache
def _declared_metric_names() -> frozenset[str]:
    names: set[str] = set()
    for path in (REPO / "openrag").rglob("*.py"):
        names.update(DECLARED_NAME.findall(path.read_text(encoding="utf-8")))
    return frozenset(names)


def test_dashboards_and_queries_are_found():
    # Guards the parametrized tests below against passing vacuously.
    assert DASHBOARD_FILES
    assert QUERIES


def test_uids_are_unique_and_linked_ones_are_kept():
    uids = [dashboard.get("uid") for dashboard in DASHBOARD_FILES.values()]
    assert all(uids), "every dashboard needs a fixed uid, or each Grafana invents its own and links break"
    assert len(uids) == len(set(uids)), f"duplicate dashboard uid: {uids}"
    assert LINKED_UIDS <= set(uids)


@pytest.mark.parametrize("name", sorted(DASHBOARD_FILES))
def test_dashboard_declares_the_datasource_variable(name: str):
    dashboard = DASHBOARD_FILES[name]
    variables = {v["name"]: v for v in dashboard.get("templating", {}).get("list", [])}
    variable = variables.get(DS_VARIABLE)
    assert variable, f"{name} has no {DS_VARIABLE} variable"
    assert variable["type"] == "datasource"
    assert variable["query"] == "prometheus"
    # An empty selection is not "the default": Grafana 11.2 and 13.2 both take
    # the first Prometheus data source in the list, which on a platform Grafana
    # with several is rarely the one scraping OpenRAG.
    assert variable.get("current", {}).get("value") == "default", f"{name} must select the default data source"
    # Only the UI's import dialog substitutes __inputs; file provisioning and the
    # ConfigMap sidecar load the JSON as-is and leave ${DS_PROMETHEUS} unresolved.
    assert "__inputs" not in dashboard, f"{name} is an external-sharing export; export it without that option"


@pytest.mark.parametrize("name", sorted(DASHBOARD_FILES))
def test_every_datasource_reference_goes_through_the_variable(name: str):
    """A literal UID resolves only in the Grafana that happens to define it."""
    dashboard = DASHBOARD_FILES[name]
    for node in _walk(dashboard):
        if "datasource" not in node:
            continue
        ref = node["datasource"]
        assert ref in (DS_REF, GRAFANA_BUILTIN), f"{name}: {node.get('title') or node.get('name')!r} binds {ref!r}"
    for panel in _panels(dashboard.get("panels", [])):
        if panel.get("type") == "row":
            continue
        # A panel without one silently follows Grafana's default data source,
        # not the one selected in the variable.
        assert panel.get("datasource") == DS_REF, f"{name}: panel {panel.get('title')!r} is not bound to ${DS_VARIABLE}"


@pytest.mark.parametrize(("where", "expr"), QUERIES, ids=[w for w, _ in QUERIES])
def test_query_does_not_hardcode_a_scrape_job(where: str, expr: str):
    """The API's job is "openrag" on Compose and the Service name on Kubernetes."""
    for match in re.finditer(r'\bjob\s*(=~|!~|=|!=)\s*"([^"]*)"', expr):
        assert match.group(2) == "$job", f"{where} pins job to {match.group(2)!r}; use the $job variable"


def test_scrape_health_is_scoped_to_the_api_job():
    """`up` has a series for every target Prometheus scrapes. Unscoped, the Service
    status card reports the worst of node-exporter, Ray and everything else rather
    than the API, and that would still pass the hardcoded-job check above.
    """
    ups = [(where, expr) for where, expr in QUERIES if UP.search(expr)]
    assert any(where.startswith("openrag-http.json:") for where, _ in ups), "Service status reads up"
    for where, expr in ups:
        assert len(UP.findall(expr)) == len(UP_ON_API_JOB.findall(expr)), f'{where} reads up without job=~"$job"'


def test_job_variable_reads_a_series_only_the_api_exports():
    """The job must come from the API's own series, or $job also matches other targets."""
    using = [name for name in DASHBOARD_FILES if any(w.startswith(f"{name}:") and "$job" in e for w, e in QUERIES)]
    assert using
    for name in using:
        variables = {v["name"]: v for v in DASHBOARD_FILES[name].get("templating", {}).get("list", [])}
        assert "job" in variables, f"{name} reads $job without defining it"
        query = variables["job"]["query"]
        query = query.get("query") if isinstance(query, dict) else query
        assert query == f"label_values({API_JOB_METRIC}, job)", f"{name}: job variable reads {query!r}"


def test_the_job_variable_selects_the_api_without_widening_up():
    """The hidden job selector must resolve per deployment and stay on the API.

    A pinned job name would be the Compose one, which no Kubernetes install
    scrapes under, so the selection comes from the query: All, which Grafana
    expands to the jobs that query returned. That holds only while the variable
    defines no All value. The endpoint and method variables beside it set
    ``.*``, and the same line here would quietly widen `up` from the API to
    every target Prometheus scrapes.
    """
    checked = 0
    for name, dashboard in DASHBOARD_FILES.items():
        for variable in dashboard.get("templating", {}).get("list", []):
            if variable["name"] != "job":
                continue
            checked += 1
            all_value = variable.get("allValue")
            assert not all_value, f"{name}: job sets allValue {all_value!r}, so All stops meaning the API's jobs"
            default = variable.get("current", {}).get("value") or []
            pinned = [v for v in ([default] if isinstance(default, str) else default) if v != ALL_VALUE]
            assert not pinned, f"{name}: job defaults to {pinned!r}, a name only one deployment scrapes under"
    assert checked


def test_an_idle_api_already_exports_the_job_series():
    """An API that has served no request must already export the series $job reads.

    Runs in a fresh interpreter: this one's registry holds the requests other
    tests made, which would hide a series that only traffic creates.
    """
    result = subprocess.run(
        [sys.executable, "-c", IDLE_API_SCRAPE],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "openrag")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sampled = {
        re.split(r"[{ ]", line, maxsplit=1)[0] for line in result.stdout.splitlines() if line[:1] not in ("", "#")
    }
    # Otherwise the scrape was not idle and the assertion below proves nothing.
    assert "openrag_http_requests_total" not in sampled
    assert API_JOB_METRIC in sampled, f"an idle API exports no {API_JOB_METRIC} sample, so $job resolves to nothing"


@pytest.mark.parametrize(("where", "expr"), QUERIES, ids=[w for w, _ in QUERIES])
def test_query_never_touches_an_unbounded_label(where: str, expr: str):
    for label in FORBIDDEN_LABELS:
        assert not re.search(rf"\b{label}\b", expr), f"{where} references {label!r}"


@pytest.mark.parametrize(("where", "expr"), QUERIES, ids=[w for w, _ in QUERIES])
def test_counters_are_rated_before_they_are_aggregated(where: str, expr: str):
    """rate() detects a counter reset only within one series. Ray workers are one
    series each and restart, so rate(sum(...)) reads every restart as a drop.
    """
    wrong = re.search(r"\b(?:rate|irate|increase)\s*\(\s*(?:sum|max|min|avg|count)\b", expr)
    assert wrong is None, f"{where} aggregates before rating: {wrong.group(0)!r}"


@pytest.mark.parametrize(("where", "expr"), QUERIES, ids=[w for w, _ in QUERIES])
def test_query_reads_only_declared_metrics(where: str, expr: str):
    """A misspelt or not-yet-shipped metric renders as "No data", not as an error.

    Names are matched against the string literals under ``openrag/``, where every
    metric is declared. Ray prefixes what it exports with ``ray_``, and a
    histogram is queried through its ``_bucket``/``_sum``/``_count`` series.
    """
    declared = _declared_metric_names()
    for name in set(METRIC_NAME.findall(expr)):
        base = HISTOGRAM_SERIES.sub("", name)
        assert name in declared or base in declared, f"{where} reads {name}, which nothing in openrag/ declares"


INFERENCE_SELECTOR = re.compile(r"openrag_inference_requests_total\"?\s*(?:,\s*)?([^}]*)\}")
OUTCOME_MATCHER = re.compile(r'outcome=~"([^"]+)"')


def _provider_down_outcomes() -> tuple[str, str]:
    """The numerator and denominator outcome sets ``OpenRagInferenceProviderDown`` pages on."""
    import yaml

    rules = yaml.safe_load((REPO / "infra/compose/prometheus/rules/openrag-alerts.yaml").read_text(encoding="utf-8"))
    (expr,) = [
        rule["expr"]
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "OpenRagInferenceProviderDown"
    ]
    numerator, denominator, *_ = OUTCOME_MATCHER.findall(expr)
    return numerator, denominator


@pytest.mark.parametrize(
    ("where", "expr"),
    [(w, e) for w, e in QUERIES if "openrag_inference_requests_total" in e and "/" in e],
)
def test_inference_ratios_judge_the_outcomes_the_alert_judges(where: str, expr: str):
    """A ratio over every outcome counts ``cancelled``, ``rejected`` and
    ``circuit_open`` calls, which say nothing about the provider: a provider
    failing every call it served reads below ``OpenRagInferenceProviderDown``'s
    threshold on the dashboard while the alert fires. Every selector in a
    ratio, the zero fallback included, carries one of the alert's two sets."""
    numerator, denominator = _provider_down_outcomes()
    selectors = INFERENCE_SELECTOR.findall(expr)
    assert selectors, f"{where}: no inference selector parsed from {expr}"
    for selector in selectors:
        outcomes = OUTCOME_MATCHER.findall(selector)
        assert outcomes in ([numerator], [denominator]), (
            f"{where}: selector {{{selector}}} should filter outcome on {numerator!r} or {denominator!r}"
        )
