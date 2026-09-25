"""Replay synthetic series through the OpenRAG Service dashboard's alert tiles.

Each At a glance tile mirrors one alert. The structure tests in
``test_grafana_dashboards.py`` cannot tell whether a tile reads the alert's
window, applies its volume floor, or counts the states it pages on. These run
the tiles' own expressions, read from the dashboard JSON, through promtool and
assert what each one shows. The alert side of every scenario was checked
against the rules with the same series.

A tile shows ``-1`` as *Low volume*: work happened, but less than the floor the
alert needs before it judges a ratio. An empty result shows the tile's no-value
text.

Skipped rather than failed when promtool is unavailable: a local ``promtool``
binary is used if present, otherwise the Prometheus image *if it is already
pulled*. ``REQUIRE_PROMTOOL`` turns the skip into a failure where promtool is
installed on purpose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = ROOT / "infra/charts/openrag-stack/dashboards/openrag-service.json"
#: The version the Compose overlay runs.
PROMETHEUS_IMAGE = "prom/prometheus:v2.54.1"

DOCS = 'openrag_ingest_documents_total{job="openrag-ray", WorkerId="w1", status="%s"}'
CALLS = 'openrag_inference_requests_total{job="openrag", provider="%s", operation="chat", outcome="%s"}'
BREAKER = 'openrag_circuit_breaker_state{job="%s", WorkerId="%s", name="%s"}'

LOW_VOLUME = -1


def _expr(title: str) -> str:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    (panel,) = [p for p in panels if p.get("title") == title]
    (target,) = panel["targets"]
    return target["expr"]


def _series(series: str, values: str) -> dict:
    return {"series": series, "values": values}


def _shows(expr: str, at: str, *samples: tuple[str, float]) -> dict:
    return {
        "expr": expr,
        "eval_time": at,
        "exp_samples": [{"labels": labels, "value": value} for labels, value in samples],
    }


def _scenarios() -> list[dict]:
    failed = _expr("Failed documents · 5m")
    errors = _expr("Inference errors · 10m")
    breakers = _expr("Open breakers")
    timeline = _expr("Circuit breakers")
    return [
        {
            # OpenRagIngestFailureRate stays silent under 5 documents in 15 minutes.
            "name": "three of three documents failed",
            "input_series": [
                _series(DOCS % "failed", "0 0 0 0 0 1 1 2 2 3 3 3 3 3 3 3 3 3 3 3 3"),
                _series(DOCS % "completed", "0x20"),
            ],
            "promql_expr_test": [_shows(failed, "10m", ("{}", LOW_VOLUME))],
        },
        {
            # The alert fires at 30m on 27.27% over 5 minutes, while the same
            # burst is 20% of the last 15 minutes: the window must be the alert's.
            "name": "a burst of failures after steady successes",
            "input_series": [
                _series(DOCS % "completed", "0+8x40"),
                _series(DOCS % "failed", "0x20 3+3x9 30x10"),
            ],
            "promql_expr_test": [_shows(failed, "30m", ("{}", 3 / 11))],
        },
        {
            # Failures that stopped ten minutes ago: the alert has resolved.
            "name": "failures that stopped",
            "input_series": [
                _series(DOCS % "failed", "0+3x10 30x20"),
                _series(DOCS % "completed", "0+1x30"),
            ],
            "promql_expr_test": [_shows(failed, "18m", ("{}", 0))],
        },
        {
            "name": "no document finished",
            "input_series": [_series(DOCS % "failed", "0x20"), _series(DOCS % "completed", "0x20")],
            "promql_expr_test": [_shows(failed, "18m")],
        },
        {
            # OpenRagInferenceProviderDown leaves out endpoints under 5 calls in 10 minutes.
            "name": "two errors in three calls",
            "input_series": [
                _series(CALLS % ("default", "success"), "0 0 0 1 1 1 1 1 1 1 1 1 1"),
                _series(CALLS % ("default", "error"), "0 0 0 0 0 1 1 2 2 2 2 2 2"),
            ],
            "promql_expr_test": [_shows(errors, "9m", ("{}", LOW_VOLUME))],
        },
        {
            # Calls the provider never served count neither in the ratio nor
            # toward the floor.
            "name": "two errors in three calls among breaker refusals",
            "input_series": [
                _series(CALLS % ("default", "success"), "0 0 0 1 1 1 1 1 1 1 1 1 1"),
                _series(CALLS % ("default", "error"), "0 0 0 0 0 1 1 2 2 2 2 2 2"),
                _series(CALLS % ("default", "circuit_open"), "0+2x12"),
            ],
            "promql_expr_test": [_shows(errors, "9m", ("{}", LOW_VOLUME))],
        },
        {
            "name": "a quiet failing endpoint beside a busy healthy one",
            "input_series": [
                _series(CALLS % ("quiet", "error"), "0 0 1 1 2 2 3 3 3 3 3"),
                _series(CALLS % ("quiet", "success"), "0x10"),
                _series(CALLS % ("busy", "success"), "0+2x10"),
                _series(CALLS % ("busy", "error"), "0x10"),
            ],
            "promql_expr_test": [_shows(errors, "10m", ("{}", 0))],
        },
        {
            "name": "an endpoint failing 60% of its calls",
            "input_series": [
                _series(CALLS % ("default", "error"), "0+3x20"),
                _series(CALLS % ("default", "success"), "0+2x20"),
            ],
            "promql_expr_test": [_shows(errors, "15m", ("{}", 0.6))],
        },
        {
            # OpenRagCircuitBreakerOpen pages on >= 1: a trial call that keeps
            # timing out holds the breaker half-open.
            "name": "a breaker half-open",
            "input_series": [_series(BREAKER % ("openrag", "", "llm"), "2x10")],
            "promql_expr_test": [
                _shows(breakers, "8m", ("{}", 1)),
                _shows(timeline, "8m", ('{name="llm"}', 2)),
            ],
        },
        {
            # max alone would rank half-open (2) above open (1).
            "name": "one breaker open in one worker and half-open in another",
            "input_series": [
                _series(BREAKER % ("openrag-ray", "a", "embedder"), "1x10"),
                _series(BREAKER % ("openrag-ray", "b", "embedder"), "2x10"),
                _series(BREAKER % ("openrag", "", "llm"), "0x10"),
            ],
            "promql_expr_test": [
                _shows(breakers, "8m", ("{}", 1)),
                _shows(timeline, "8m", ('{name="embedder"}', 1), ('{name="llm"}', 0)),
            ],
        },
        {
            "name": "breakers closed or unknown",
            "input_series": [
                _series(BREAKER % ("openrag", "", "llm"), "0x10"),
                _series(BREAKER % ("openrag-ray", "a", "vlm"), "-1x10"),
            ],
            "promql_expr_test": [
                _shows(breakers, "8m", ("{}", 0)),
                _shows(timeline, "8m", ('{name="llm"}', 0), ('{name="vlm"}', -1)),
            ],
        },
    ]


def _promtool(test_file: Path) -> list[str] | None:
    binary = shutil.which("promtool")
    if binary:
        return [binary, "test", "rules", str(test_file)]
    docker = shutil.which("docker")
    if not docker:
        return None
    # Only if the image is already local: a unit-test run must not pull it.
    present = subprocess.run([docker, "image", "inspect", PROMETHEUS_IMAGE], capture_output=True, check=False)
    if present.returncode != 0:
        return None
    return [
        docker,
        "run",
        "--rm",
        "-v",
        f"{test_file.parent}:/tests:ro",
        "--entrypoint",
        "promtool",
        PROMETHEUS_IMAGE,
        "test",
        "rules",
        f"/tests/{test_file.name}",
    ]


def test_alert_tiles_show_what_their_alerts_evaluate(tmp_path: Path) -> None:
    test_file = tmp_path / "service_dashboard_tiles.promtool.json"
    tests = [{"interval": "1m", **scenario} for scenario in _scenarios()]
    # JSON is YAML, which promtool reads.
    test_file.write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": tests}, indent=1))
    # The image runs promtool as nobody, and pytest creates tmp_path private.
    tmp_path.chmod(0o755)
    test_file.chmod(0o644)

    command = _promtool(test_file)
    if command is None:
        if os.environ.get("REQUIRE_PROMTOOL", "").strip().lower() in {"1", "true", "yes"}:
            pytest.fail("REQUIRE_PROMTOOL is set but no promtool is available")
        pytest.skip(f"needs a promtool binary, or the {PROMETHEUS_IMAGE} image already pulled")

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, f"promtool unit tests failed:\n{result.stdout}\n{result.stderr}"
    assert "SUCCESS" in result.stdout
