"""Guards on the chart's scrape wiring for Ray, Postgres and Milvus.

Every failure mode here is silent: a monitor pointed at a port name nothing
declares, or a Ray node back on KubeRay's default port, still renders and
installs. The static checks read the chart as text and run anywhere. Reading
text cannot tell whether a selector matches any pod, so the rendered checks
below run the chart through Helm, with the Postgres and Milvus sub-charts where
their pods are what a policy must match.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from textwrap import indent

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
TEMPLATES = CHART_DIR / "templates"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")


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


@pytest.mark.parametrize("role", ["primary", "readReplicas"])
def test_postgres_subchart_policy_does_not_open_the_exporter_to_every_source(role: str) -> None:
    """bitnami's own policy, with its allowExternal default, admits any source on 5432 and 9187.
    The read replicas carry a second copy, rendered with architecture: replication."""
    policy = _values()["postgresql"][role]["networkPolicy"]
    assert policy.get("enabled") is False or policy.get("allowExternal") is False


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


def _pod_monitor_metric_relabelings() -> list[dict]:
    template = _template("datastore-metrics.yaml")
    start = template.index("      metricRelabelings:")
    block = template[start : template.index("{{- end }}", start)]
    return yaml.safe_load(textwrap.dedent(block))["metricRelabelings"]


def test_pod_monitor_stores_openrag_series_under_their_unprefixed_names() -> None:
    """Ray prefixes ray.util.metrics names with ray_; the alert rules query the bare names."""
    rules = _pod_monitor_metric_relabelings()
    # WorkerId keeps concurrent workers' series apart: without it one scrape
    # carries duplicate samples and Prometheus rejects all of it.
    assert not [r for r in rules if r.get("action") in ("labeldrop", "labelkeep")]

    def relabel(name: str) -> str:
        for rule in rules:
            if rule.get("action", "replace") != "replace" or rule.get("targetLabel") != "__name__":
                continue
            assert rule["sourceLabels"] == ["__name__"]
            # Prometheus anchors the regex and writes $1 for a group reference.
            match = re.fullmatch(rule["regex"], name)
            if match:
                name = match.expand(rule["replacement"].replace("$", "\\"))
        return name

    assert relabel("ray_openrag_ingest_documents_total") == "openrag_ingest_documents_total"
    assert relabel("ray_openrag_ingest_stage_duration_seconds_bucket") == (
        "openrag_ingest_stage_duration_seconds_bucket"
    )
    # Ray's own metrics keep the names its bundled dashboards query.
    assert relabel("ray_node_cpu_utilization") == "ray_node_cpu_utilization"


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


# Rendered checks. The chart is copied without the sub-charts it does not need:
# they are fetched archives (`helm dependency build`), not in the repository.
REQUIRED_SECRETS = [
    "--set",
    "env.secrets.AUTH_TOKEN=or-unit-test-token-0123",
    "--set",
    "postgresql.auth.password=unit-test-password-0123",
]
POSTGRES_SUPERUSER = ["--set", "postgresql.auth.postgresPassword=unit-test-superuser-0123"]
METRICS_FROM = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}]
RAY_MONITOR = ["--set", "ray.enabled=true", "--set", "ray.metrics.podMonitor.enabled=true"]
POSTGRES_MONITOR = [
    "--set",
    "postgresql.metrics.enabled=true",
    *POSTGRES_SUPERUSER,
    "--set",
    "postgresql.metrics.serviceMonitor.enabled=true",
]
MILVUS_MONITOR = ["--set", "milvus.metrics.serviceMonitor.enabled=true"]
EVERY_MONITOR = [*RAY_MONITOR, *POSTGRES_MONITOR, *MILVUS_MONITOR]
EVERY_EXPORTER = [*EVERY_MONITOR, "--set-json", f"networkPolicy.metricsFrom={json.dumps(METRICS_FROM)}"]
DATASTORES = ("postgresql", "milvus")


def _archive(name: str) -> Path | None:
    return next(iter(sorted((CHART_DIR / "charts").glob(f"{name}-[0-9]*.tgz"))), None)


requires_datastore_charts = pytest.mark.skipif(
    HELM is None or not all(_archive(name) for name in DATASTORES),
    reason="needs Helm and the postgresql and milvus archives (helm dependency build)",
)


def _chart(tmp_path: Path, *subcharts: str) -> Path:
    chart = tmp_path / "openrag-stack"
    shutil.copytree(TEMPLATES, chart / "templates")
    shutil.copytree(CHART_DIR / "dashboards", chart / "dashboards")
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    meta = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    meta["dependencies"] = [d for d in meta["dependencies"] if d["name"] in subcharts]
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (chart / "charts").mkdir()
    for name in subcharts:
        shutil.copy(_archive(name), chart / "charts")
    return chart


def _render(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "openrag", str(chart), "--namespace", "rag", *REQUIRED_SECRETS, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _objects(chart: Path, *args: str) -> list[dict]:
    result = _render(chart, *args)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _pods(objects: list[dict]) -> list[tuple[str, dict, list[dict]]]:
    """(name, labels, containers) for every pod the objects ask for. KubeRay
    creates the Ray pods itself and labels them with their cluster and node type,
    so those labels are added here as KubeRay would."""
    pods = []
    for obj in objects:
        name = f"{obj['kind']}/{obj['metadata']['name']}"
        if obj["kind"] in ("Deployment", "StatefulSet", "DaemonSet"):
            template = obj["spec"]["template"]
            pods.append((name, template["metadata"].get("labels") or {}, template["spec"]["containers"]))
        elif obj["kind"] == "RayCluster":
            groups = [("head", obj["spec"]["headGroupSpec"])]
            groups += [("worker", group) for group in obj["spec"].get("workerGroupSpecs", [])]
            for node_type, group in groups:
                template = group["template"]
                labels = {
                    **((template.get("metadata") or {}).get("labels") or {}),
                    "ray.io/cluster": obj["metadata"]["name"],
                    "ray.io/node-type": node_type,
                    "ray.io/is-ray-node": "yes",
                }
                pods.append((f"{name}/{node_type}", labels, template["spec"]["containers"]))
    return pods


def _selected(pods: list[tuple[str, dict, list[dict]]], match_labels: dict) -> list[tuple[str, dict, list[dict]]]:
    return [pod for pod in pods if match_labels.items() <= pod[1].items()]


def _container_ports(containers: list[dict]) -> list[dict]:
    return [port for container in containers for port in container.get("ports", [])]


@requires_datastore_charts
def test_each_metrics_policy_opens_a_port_every_pod_it_selects_exports(tmp_path: Path) -> None:
    """A misspelled selector opens nothing and a wrong port opens a dead one:
    both render, and both leave the targets down behind the default-deny."""
    objects = _objects(_chart(tmp_path, *DATASTORES), *EVERY_EXPORTER)
    pods = _pods(objects)
    policies = [p for p in objects if p["kind"] == "NetworkPolicy" and p["metadata"]["name"].endswith("-metrics")]

    assert sorted(p["metadata"]["name"] for p in policies) == [
        "openrag-milvus-metrics",
        "openrag-postgresql-metrics",
        "openrag-raycluster-metrics",
    ]
    for policy in policies:
        name = policy["metadata"]["name"]
        (rule,) = policy["spec"]["ingress"]
        assert rule["from"] == METRICS_FROM
        (port,) = [entry["port"] for entry in rule["ports"]]
        selected = _selected(pods, policy["spec"]["podSelector"]["matchLabels"])
        assert selected, f"{name} selects no pod"
        for pod_name, _, containers in selected:
            assert port in [p["containerPort"] for p in _container_ports(containers)], (
                f"{name} opens {port}, which {pod_name} does not export"
            )


@requires_helm
def test_the_ray_pod_monitor_scrapes_head_and_workers_where_they_export(tmp_path: Path) -> None:
    """The port the PodMonitor names must be the one each node's ray start listens on."""
    objects = _objects(_chart(tmp_path), *RAY_MONITOR)
    (monitor,) = [obj for obj in objects if obj["kind"] == "PodMonitor"]
    (cluster,) = [obj for obj in objects if obj["kind"] == "RayCluster"]
    (endpoint,) = monitor["spec"]["podMetricsEndpoints"]

    head_args = cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["args"]
    exported = {"head": int(next(a for a in head_args if "--metrics-export-port" in a).rsplit("=", 1)[1])}
    for group in cluster["spec"]["workerGroupSpecs"]:
        exported["worker"] = int(group["rayStartParams"]["metrics-export-port"])

    selected = _selected(_pods(objects), monitor["spec"]["selector"]["matchLabels"])
    assert sorted(labels["ray.io/node-type"] for _, labels, _ in selected) == ["head", "worker"]
    for pod_name, labels, containers in selected:
        named = [p["containerPort"] for p in _container_ports(containers) if p.get("name") == endpoint["port"]]
        assert named == [exported[labels["ray.io/node-type"]]], pod_name


@requires_datastore_charts
def test_no_postgres_policy_of_the_subchart_is_rendered(tmp_path: Path) -> None:
    """Under replication the read replicas get their own copy of bitnami's
    allow-any policy, and NetworkPolicies add up: the chart's rules decide only
    while no other policy selects the pods."""
    objects = _objects(_chart(tmp_path, *DATASTORES), *EVERY_EXPORTER, "--set", "postgresql.architecture=replication")
    policies = [obj for obj in objects if obj["kind"] == "NetworkPolicy"]

    assert policies
    for policy in policies:
        assert policy["metadata"]["labels"]["helm.sh/chart"].startswith("openrag-stack-"), policy["metadata"]["name"]


@requires_helm
@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--set", "ray.metrics.podMonitor.enabled=true"], "ray.metrics.podMonitor.enabled requires ray.enabled=true"),
        (
            ["--set", "postgresql.metrics.serviceMonitor.enabled=true"],
            "postgresql.metrics.serviceMonitor.enabled requires postgresql.metrics.enabled=true",
        ),
        (
            ["--set", "milvus.metrics.enabled=false", "--set", "milvus.metrics.serviceMonitor.enabled=true"],
            "milvus.metrics.serviceMonitor.enabled requires milvus.metrics.enabled=true",
        ),
        (
            ["--set", "postgresql.metrics.enabled=true"],
            "postgresql.metrics.enabled requires postgresql.auth.postgresPassword or postgresql.auth.existingSecret",
        ),
    ],
    ids=["ray", "postgres-monitor", "milvus-monitor", "postgres-password"],
)
def test_settings_that_would_scrape_nothing_fail_the_render(tmp_path: Path, args: list[str], error: str) -> None:
    result = _render(_chart(tmp_path), *args)

    assert result.returncode != 0
    assert error in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "credential",
    [POSTGRES_SUPERUSER, ["--set", "postgresql.auth.existingSecret=postgres-credentials"]],
    ids=["password", "existing-secret"],
)
def test_the_postgres_exporter_renders_with_a_fixed_superuser_password(tmp_path: Path, credential: list[str]) -> None:
    """Either one survives a render that cannot read the live Secret."""
    result = _render(_chart(tmp_path), "--set", "postgresql.metrics.enabled=true", *credential)

    assert result.returncode == 0, result.stderr


def _notes(tmp_path: Path, *args: str) -> str:
    """`helm template` never prints NOTES.txt, so it is rendered under a second
    name, indented under a block scalar so that Helm parses it as YAML."""
    chart = _chart(tmp_path)
    notes = (chart / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    (chart / "templates" / "notes-under-test.yaml").write_text("notes: |\n" + indent(notes, "  "), encoding="utf-8")

    result = _render(chart, *args, "-s", "templates/notes-under-test.yaml")

    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)["notes"]


@requires_helm
def test_the_notes_warn_about_unlabelled_monitors_and_an_empty_metrics_from(tmp_path: Path) -> None:
    notes = _notes(tmp_path, *EVERY_MONITOR)

    assert "⚠  ray.metrics.podMonitor.labels is empty" in notes
    assert "⚠  postgresql.metrics.serviceMonitor.labels is empty" in notes
    assert "⚠  milvus.metrics.serviceMonitor.additionalLabels is empty" in notes
    assert "⚠  networkPolicy.metricsFrom is empty" in notes


@requires_helm
def test_the_notes_do_not_warn_under_the_bundled_prometheus(tmp_path: Path) -> None:
    """It runs in the release namespace and selects every monitor. Warnings it
    does not need teach operators to skip the ones that matter."""
    notes = _notes(
        tmp_path,
        *EVERY_MONITOR,
        "--set",
        "monitoring.bundled=true",
        "--set",
        "env.secrets.METRICS_TOKEN=unit-test-metrics-token",
    )

    assert "Datastore metrics" in notes
    assert "⚠" not in notes.split("Datastore metrics", 1)[1].split("Monitoring (bundled", 1)[0]


@requires_helm
def test_an_upgrade_names_the_ray_workers_to_recreate_and_leaves_the_head(tmp_path: Path) -> None:
    """Every Ray install is affected, whether or not it scrapes anything."""
    notes = _notes(tmp_path, "--is-upgrade", "--set", "ray.enabled=true", "--set", "fullnameOverride=rag")

    assert "kubectl delete pod -n rag -l ray.io/cluster=rag-raycluster,ray.io/node-type=worker" in notes
    assert 'Postgres now accepts connections only from namespace "rag"' in notes


@requires_helm
def test_a_first_install_prints_no_upgrade_steps(tmp_path: Path) -> None:
    assert "Upgrading from chart" not in _notes(tmp_path, "--set", "ray.enabled=true")
