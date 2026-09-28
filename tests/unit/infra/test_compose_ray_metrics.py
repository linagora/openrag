"""The Compose monitoring overlay must collect what Ray actors record.

Indexing, and every inference call the indexing workers make, record their
metrics in Ray actors. Those reach Prometheus only through Ray's metrics agent,
never through the API's /metrics. The path spans three files that must agree:
api/main.py hands the embedded Ray a fixed port, the overlay sets it, and
prometheus.yml scrapes it. Any one of them drifting leaves the ingestion panels
empty with no error anywhere.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
MAIN = REPO / "openrag/api/main.py"
OVERLAY = REPO / "infra/compose/monitoring.docker-compose.yaml"
PROMETHEUS = REPO / "infra/compose/prometheus/prometheus.yml"
BASE = REPO / "infra/compose/docker-compose.yaml"

API_SERVICES = ("openrag", "openrag-cpu")
PORT_VARIABLE = "RAY_METRICS_EXPORT_PORT"

# The jobs OpenRagTargetDown watches.
TARGET_DOWN_JOBS = ".*openrag.*"


def _embedded_ray_init(tree: ast.Module) -> ast.Call:
    """The ray.init call that starts the embedded cluster: the one given a dashboard host."""
    embedded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "init"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ray"
        and any(kw.arg == "dashboard_host" for kw in node.keywords)
    ]
    assert len(embedded) == 1, "expected one embedded ray.init(dashboard_host=...) in api/main.py"
    return embedded[0]


def _calls_to(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
    ]


def _overlay_port() -> str:
    overlay = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))["services"]
    ports = {service: overlay[service]["environment"][PORT_VARIABLE] for service in API_SERVICES}
    assert len(set(ports.values())) == 1, f"API variants disagree on the Ray metrics port: {ports}"
    return next(iter(ports.values()))


def _ray_job() -> dict:
    jobs = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))["scrape_configs"]
    targets = {f"openrag:{_overlay_port()}"}
    matching = [job for job in jobs if any(targets & set(c["targets"]) for c in job.get("static_configs", []))]
    assert len(matching) == 1, f"expected one scrape job on {targets}, found {len(matching)}"
    return matching[0]


def test_embedded_ray_is_given_the_configured_metrics_port():
    """Without a port Ray picks a random one, which no scrape config can name."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    assert "_metrics_export_port" in {kw.arg for kw in _embedded_ray_init(tree).keywords}
    assert f'"{PORT_VARIABLE}"' in MAIN.read_text(encoding="utf-8")


def test_ray_serve_starts_ray_with_the_same_settings():
    """serve.start() starts Ray itself when nothing has, with none of these
    settings, and every Serve replica then finds Ray running and skips the
    lifespan's call. The launcher must run the same initializer first, or
    ENABLE_RAY_SERVE=true silently drops RAY_METRICS_EXPORT_PORT.
    """
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    init = _embedded_ray_init(tree)
    initializer = next(
        func
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef) and init in ast.walk(func)
    )
    lifespan = next(f for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef) and f.name == "lifespan")
    assert _calls_to(lifespan, initializer.name), f"the lifespan does not call {initializer.name}()"

    launcher = next(
        node
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"'
    )
    starts = [
        call
        for call in ast.walk(launcher)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "start"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "serve"
    ]
    assert len(starts) == 1, "expected one serve.start() in the launcher"
    before = [call for call in _calls_to(launcher, initializer.name) if call.lineno < starts[0].lineno]
    assert before, f"the Serve launcher calls serve.start() without calling {initializer.name}() first"


def test_the_ray_job_is_watched_by_the_target_down_alert():
    """OpenRagTargetDown watches the jobs matching job=~".*openrag.*". Outside it,
    the Ray scrape can fail unnoticed: the ingestion tiles then read Idle rather
    than anything that looks broken.
    """
    assert re.fullmatch(TARGET_DOWN_JOBS, _ray_job()["job_name"])


def test_overlay_pins_the_port_on_both_api_variants():
    assert re.fullmatch(r"\d+", _overlay_port())


def test_overlay_does_not_publish_the_port():
    """The metrics agent has no authentication and binds every interface."""
    overlay = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))["services"]
    port = _overlay_port()
    base = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    published = [str(p) for p in base["x-openrag"]["ports"]]
    for service in API_SERVICES:
        published += [str(p) for p in overlay[service].get("ports", [])]
    assert not [p for p in published if p.endswith(f":{port}") or p == port], published


def test_prometheus_scrapes_the_port_on_the_shared_alias():
    """Both API variants carry the `openrag` network alias, so one target covers either."""
    job = _ray_job()
    assert job.get("metrics_path", "/metrics") == "/metrics"
    base = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    assert "openrag" in base["x-openrag"]["networks"]["default"]["aliases"]


def test_ray_prefix_is_stripped_from_openrag_series_only():
    """Stored under the names the API exports and the alert rules query; Ray's own
    ray_* metrics keep theirs. A rename that also dropped WorkerId would collapse
    concurrent workers into duplicate samples and lose the whole scrape.
    """
    relabels = _ray_job().get("metric_relabel_configs", [])
    rename = [r for r in relabels if r.get("target_label") == "__name__"]
    assert rename == [
        {"source_labels": ["__name__"], "regex": "ray_(openrag_.+)", "target_label": "__name__", "replacement": "$1"}
    ]
    assert not [r for r in relabels if r.get("action") in ("labeldrop", "labelkeep", "drop")]
