"""The canary's alert: well-formed, wired into both deployments, and about real series."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from core.observability.monitoring import CanaryMetrics
from prometheus_client import CollectorRegistry

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
RULES_DIR = CHART_DIR / "rules"
COMPOSE_DIR = ROOT / "infra" / "compose"
DOCS_DIR = ROOT / "docs" / "content" / "docs"
PROMTOOL_TEST = Path(__file__).parent / "prometheus" / "openrag-canary.rules.test.yaml"
DOCS_SITE = "https://linagora.github.io/openrag/"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
PROMTOOL = os.environ.get("PROMTOOL_BIN") or shutil.which("promtool")


def _rules() -> list[dict]:
    rules = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            rules.extend(group["rules"])
    return rules


def test_the_canary_alert_exists():
    # A canary nobody alerts on is worse than none: a dashboard shows it
    # passing right up until nobody notices it stopped.
    assert {rule.get("alert") for rule in _rules()} >= {"OpenRagCanaryFailing"}


@pytest.mark.parametrize("rule", _rules(), ids=lambda rule: f"{rule['alert']}-{rule['labels'].get('condition')}")
def test_every_alert_links_a_runbook_page_that_exists(rule):
    url = rule["annotations"]["runbook_url"]
    assert url.startswith(DOCS_SITE), url
    slug = url.removeprefix(DOCS_SITE).strip("/")
    assert (DOCS_DIR / f"{slug}.md").is_file(), f"{url} has no page under {DOCS_DIR}"
    assert rule["labels"]["severity"]


def test_alert_expressions_only_read_series_the_api_exports():
    # A renamed metric turns an alert into one that can never fire.
    registry = CollectorRegistry()
    CanaryMetrics(registry=registry)
    exported = {metric.name for metric in registry.collect()} | {
        sample.name for metric in registry.collect() for sample in metric.samples
    }
    for rule in _rules():
        for name in re.findall(r"\bopenrag_[a-z_]+\b", rule["expr"]):
            assert name in exported, f"{rule['alert']} reads {name}, which nothing exports"


def test_compose_prometheus_loads_the_charts_rules():
    config = yaml.safe_load((COMPOSE_DIR / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    assert "/etc/prometheus/rules/*.yaml" in config["rule_files"]
    compose = yaml.safe_load((COMPOSE_DIR / "monitoring.docker-compose.yaml").read_text(encoding="utf-8"))
    mounts = compose["services"]["prometheus"]["volumes"]
    assert "../charts/openrag-stack/rules:/etc/prometheus/rules:ro" in mounts
    assert (COMPOSE_DIR / "../charts/openrag-stack/rules").resolve() == RULES_DIR.resolve()


def test_the_chart_enables_the_canary_its_rules_alert_on():
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert values["env"]["config"]["CANARY_ENABLED"] == "true"
    assert values["openrag"]["metrics"]["prometheusRule"]["enabled"] is False


def _isolated_chart(tmp_path: Path) -> Path:
    chart = tmp_path / "openrag-stack"
    (chart / "templates").mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for template in ("_helpers.tpl", "prometheusrule.yaml"):
        shutil.copy(CHART_DIR / "templates" / template, chart / "templates" / template)
    shutil.copytree(RULES_DIR, chart / "rules")
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


@pytest.mark.skipif(HELM is None, reason="Helm is not installed")
def test_the_chart_ships_the_same_rules_with_routing_labels_added(tmp_path):
    rendered = subprocess.run(
        [
            HELM,
            "template",
            "test",
            str(_isolated_chart(tmp_path)),
            "--show-only",
            "templates/prometheusrule.yaml",
            "--set",
            "openrag.metrics.prometheusRule.enabled=true",
            "--set",
            "openrag.metrics.prometheusRule.labels.release=kube-prometheus-stack",
            "--set",
            "openrag.metrics.prometheusRule.ruleLabels.team=openrag",
            "--set",
            "openrag.metrics.prometheusRule.ruleLabels.severity=info",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    manifest = yaml.safe_load(rendered)

    assert manifest["kind"] == "PrometheusRule"
    assert manifest["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    shipped = [rule for group in manifest["spec"]["groups"] for rule in group["rules"]]
    source = _rules()
    assert len(shipped) == len(source)
    for rendered_rule, rule in zip(shipped, source, strict=True):
        assert rendered_rule["expr"] == rule["expr"]
        assert rendered_rule["annotations"] == rule["annotations"]
        # Routing labels are added; the rule's own labels are never overridden.
        assert rendered_rule["labels"] == {**rule["labels"], "team": "openrag"}


@pytest.mark.skipif(PROMTOOL is None, reason="promtool is not installed")
def test_the_alert_fires_on_the_conditions_it_claims():
    result = subprocess.run(
        [PROMTOOL, "test", "rules", PROMTOOL_TEST.name],
        cwd=PROMTOOL_TEST.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
