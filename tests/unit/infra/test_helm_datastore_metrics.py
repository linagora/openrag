"""Guards on the chart's scrape wiring for Ray, Postgres and Milvus (#931).

Every failure mode here is silent: a monitor pointed at a port name nothing
declares, or a Ray node back on KubeRay's default port, still renders and
installs. Static checks in the style of the other infra tests — no helm binary,
no cluster.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
TEMPLATES = CHART_DIR / "templates"


def _values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _ray_metrics_port() -> int:
    helpers = _template("_helpers.tpl")
    match = re.search(r'define "openrag-stack\.rayMetricsPort" -}}\s*(\d+)', helpers)
    assert match, "openrag-stack.rayMetricsPort no longer defines a literal port"
    return int(match.group(1))


def test_ray_metrics_port_is_not_a_public_port() -> None:
    """The externalPorts rule matches by port number on every pod in the namespace."""
    assert _ray_metrics_port() not in _values()["networkPolicy"]["externalPorts"]


def test_workers_override_kuberays_default_metrics_port() -> None:
    """Workers run $KUBERAY_GEN_RAY_START_CMD, which defaults to --metrics-export-port=8080."""
    raycluster = _template("raycluster.yaml")
    workers = raycluster[raycluster.index("workerGroupSpecs:") :]
    assert 'metrics-export-port: "{{ include "openrag-stack.rayMetricsPort" $ }}"' in workers
    assert '"--metrics-export-port={{ include "openrag-stack.rayMetricsPort" . }}"' in raycluster


def test_head_and_workers_declare_the_port_name_kuberay_and_the_podmonitor_use() -> None:
    """KubeRay appends its own `metrics: 8080` to any Ray container without a port of that name."""
    raycluster = _template("raycluster.yaml")
    declared = re.findall(
        r'- containerPort: \{\{ include "openrag-stack\.rayMetricsPort" [.$] \}\}\s+name: (\S+)',
        raycluster,
    )
    assert declared == ["metrics", "metrics"], f"head and worker metrics ports: {declared}"
    assert "- port: metrics" in _template("datastore-metrics.yaml")


def test_scrape_wiring_is_off_by_default() -> None:
    """The monitors need the Operator's CRDs, and the Postgres exporter restarts Postgres."""
    values = _values()
    assert values["networkPolicy"]["metricsFrom"] == []
    assert values["ray"]["metrics"]["podMonitor"]["enabled"] is False
    assert values["postgresql"]["metrics"]["enabled"] is False
    assert values["postgresql"]["metrics"]["serviceMonitor"]["enabled"] is False
    assert values["milvus"]["metrics"]["serviceMonitor"]["enabled"] is False


def test_monitor_label_keys_match_what_each_chart_reads() -> None:
    """A mistyped key is ignored, and the unlabelled monitor is never selected."""
    values = _values()
    assert "labels" in values["ray"]["metrics"]["podMonitor"]
    assert "labels" in values["postgresql"]["metrics"]["serviceMonitor"]
    assert "additionalLabels" in values["milvus"]["metrics"]["serviceMonitor"]
    assert ".Values.ray.metrics.podMonitor.labels" in _template("datastore-metrics.yaml")
