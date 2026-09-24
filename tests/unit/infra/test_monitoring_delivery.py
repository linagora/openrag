"""One set of dashboards, delivered three ways.

The dashboards live once, in the chart's ``dashboards/`` directory, because Helm's
``.Files.Glob`` reads nothing outside the chart. The Compose overlay mounts that
directory, and the chart renders it as sidecar ConfigMaps, for a platform's
Grafana (integrated) or the kube-prometheus-stack it bundles (standalone). A
second copy is how the Compose and Kubernetes dashboards drift apart without
anyone noticing, so these tests pin the single copy and each delivery path.

The chart tests render an isolated copy with the sub-charts stripped: they are
fetched archives, not in the repository, and the parent templates under test do
not read them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from textwrap import indent

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
DASHBOARDS = CHART_DIR / "dashboards"
COMPOSE_DIR = ROOT / "infra" / "compose"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

# A client-side apply (kubectl apply, Argo CD's default) copies the whole object
# into the last-applied-configuration annotation, capped at 256 KiB. The JSON is
# escaped into that annotation, so keep a margin below the cap.
MAX_DASHBOARD_BYTES = 200 * 1024

REQUIRED_SECRETS = [
    "--set",
    "env.secrets.AUTH_TOKEN=or-unit-test-token-0123",
    "--set",
    "postgresql.auth.password=unit-test-password-0123",
]
METRICS_TOKEN = ["--set", "env.secrets.METRICS_TOKEN=unit-test-metrics-token"]
RAY_SERVE = ["--set", "ray.enabled=true", "--set-string", "env.config.ENABLE_RAY_SERVE=true"]


def _values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))


def _dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS.glob("*.json"))


def test_the_chart_holds_the_dashboards():
    # Guards everything below against passing on an empty directory.
    assert _dashboard_files()


def test_no_second_copy_of_the_dashboards():
    """A dashboard added under the old Compose path would load on Compose only."""
    strays = sorted(path.relative_to(ROOT) for path in (COMPOSE_DIR / "grafana").rglob("*.json"))
    assert not strays, f"dashboards belong in {DASHBOARDS.relative_to(ROOT)}, not {strays}"


def test_compose_mounts_the_chart_dashboards():
    overlay = yaml.safe_load((COMPOSE_DIR / "monitoring.docker-compose.yaml").read_text(encoding="utf-8"))
    provider = yaml.safe_load(
        (COMPOSE_DIR / "grafana/provisioning/dashboards/dashboard.yml").read_text(encoding="utf-8")
    )["providers"][0]

    mounts = [volume.split(":") for volume in overlay["services"]["grafana"]["volumes"]]
    sources = {target: source for source, target, *_ in mounts}
    target = provider["options"]["path"]

    assert target in sources, f"nothing is mounted where the provisioning reads ({target})"
    # Relative paths in a Compose file resolve against its own directory.
    assert (COMPOSE_DIR / sources[target]).resolve() == DASHBOARDS.resolve()


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_each_dashboard_fits_a_client_side_apply(path: Path):
    size = path.stat().st_size
    assert size <= MAX_DASHBOARD_BYTES, f"{path.name} is {size} bytes; split it rather than outgrow a ConfigMap apply"


def test_kube_prometheus_stack_is_opt_in():
    """An integrated install must never plant a second Prometheus Operator."""
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    dependency = next(d for d in chart["dependencies"] if d["name"] == "kube-prometheus-stack")
    values = _values()

    assert dependency["condition"] == "monitoring.bundled"
    assert values["monitoring"]["bundled"] is False
    assert values["monitoring"]["dashboards"]["enabled"] is False
    # Values reach a sub-chart under its alias; a key under the real name would
    # be silently ignored.
    assert dependency["alias"] in values
    assert "kube-prometheus-stack" not in values


def test_bundled_grafana_loads_what_the_chart_labels():
    """The bundled sidecar must watch the label and folder annotation the
    ConfigMaps carry, or the dashboards are created and never shown."""
    values = _values()
    sidecar = values["kubePrometheusStack"]["grafana"]["sidecar"]["dashboards"]
    dashboards = values["monitoring"]["dashboards"]

    assert (sidecar["label"], sidecar["labelValue"]) == (dashboards["label"], dashboards["labelValue"])
    assert sidecar["folderAnnotation"] == dashboards["folderAnnotation"]
    assert sidecar["provider"]["foldersFromFilesStructure"] is True


def test_bundled_prometheus_selects_monitors_without_a_release_label():
    """Otherwise the chart's monitors, and the GPU Operator's, need a label nobody sets."""
    spec = _values()["kubePrometheusStack"]["prometheus"]["prometheusSpec"]

    assert spec["serviceMonitorSelectorNilUsesHelmValues"] is False
    assert spec["podMonitorSelectorNilUsesHelmValues"] is False
    assert spec["ruleSelectorNilUsesHelmValues"] is False


# Every workload the bundled stack runs, by the kubePrometheusStack key that
# sizes it. The config-reloader entry covers the sidecar of Prometheus and of
# Alertmanager; the grafana.sidecar one, both of Grafana's; the
# admissionWebhooks.patch one, both certificate hook Jobs.
BUNDLED_WORKLOADS = [
    ("prometheus", "prometheusSpec"),
    ("alertmanager", "alertmanagerSpec"),
    ("prometheusOperator",),
    ("prometheusOperator", "admissionWebhooks", "deployment"),
    ("prometheusOperator", "admissionWebhooks", "patch"),
    ("prometheusOperator", "prometheusConfigReloader"),
    ("grafana",),
    ("grafana", "sidecar"),
    ("kube-state-metrics",),
    ("prometheus-node-exporter",),
]


def _mebibytes(quantity: str) -> int:
    number, unit = re.fullmatch(r"(\d+)(Mi|Gi)", quantity).groups()
    return int(number) * (1024 if unit == "Gi" else 1)


@pytest.mark.parametrize("path", BUNDLED_WORKLOADS, ids=".".join)
def test_every_bundled_workload_reserves_what_it_uses(path: tuple[str, ...]):
    """Upstream sets none, which leaves the pods BestEffort: nothing reserved,
    and the first evicted under memory pressure — monitoring gone exactly when a
    node is in trouble."""
    node = _values()["kubePrometheusStack"]
    for key in path:
        node = node[key]
    resources = node["resources"]

    assert resources["requests"].keys() == {"cpu", "memory"}
    # Memory only: a CPU limit throttles scrapes and rule evaluation.
    assert resources["limits"].keys() == {"memory"}
    assert _mebibytes(resources["limits"]["memory"]) > _mebibytes(resources["requests"]["memory"])


CONTROL_PLANE = ["kubeControllerManager", "kubeScheduler", "kubeEtcd", "kubeProxy"]


@pytest.mark.parametrize("component", CONTROL_PLANE)
def test_bundled_prometheus_leaves_the_control_plane_alone(component: str):
    """A managed control plane runs these out of reach, so their absent(up)
    alerts would fire forever; a self-managed one turns them back on."""
    assert _values()["kubePrometheusStack"][component]["enabled"] is False


def _isolated_chart(tmp_path: Path) -> Path:
    chart = tmp_path / "openrag-stack"
    shutil.copytree(CHART_DIR / "templates", chart / "templates")
    shutil.copytree(DASHBOARDS, chart / "dashboards")
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    meta = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    meta.pop("dependencies", None)
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    return chart


def _render(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(chart), "--namespace", "openrag", *REQUIRED_SECRETS, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _objects(rendered: str, kind: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == kind]


def _dashboard_configmaps(rendered: str) -> list[dict]:
    return [cm for cm in _objects(rendered, "ConfigMap") if "-dashboard-" in cm["metadata"]["name"]]


@requires_helm
def test_nothing_is_rendered_by_default(tmp_path: Path):
    result = _render(_isolated_chart(tmp_path))

    assert result.returncode == 0, result.stderr
    assert not _dashboard_configmaps(result.stdout)
    assert not _objects(result.stdout, "ServiceMonitor")


@requires_helm
def test_one_configmap_per_dashboard_holding_the_file(tmp_path: Path):
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.dashboards.enabled=true")

    assert result.returncode == 0, result.stderr
    configmaps = _dashboard_configmaps(result.stdout)
    by_key = {key: (cm, content) for cm in configmaps for key, content in cm["data"].items()}
    assert sorted(by_key) == [path.name for path in _dashboard_files()]
    for path in _dashboard_files():
        configmap, content = by_key[path.name]
        # The sidecar writes the value out as the file Grafana loads.
        assert content == path.read_text(encoding="utf-8").rstrip("\n")
        assert configmap["metadata"]["labels"]["grafana_dashboard"] == "1"
        assert configmap["metadata"]["annotations"] == {"grafana_folder": "OpenRAG"}
        assert "namespace" not in configmap["metadata"]


# The name a release is pinned to, at the longest the fullname helper allows.
# Long enough that a 63-character cut would reach into the dashboard's own name.
LONG_FULLNAME = "openrag-platform-production-tenant-alpha-monitoring-stack-0001"
DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


@requires_helm
@pytest.mark.parametrize("fullname", ["openrag", LONG_FULLNAME], ids=["default", "long"])
def test_configmap_names_stay_unique(tmp_path: Path, fullname: str):
    """What tells the ConfigMaps apart is the dashboard name at the end. Cut the
    name short and two dashboards collide, and Helm installs one instead of two.
    A name is a DNS subdomain (253), not a label value (63), so nothing here is
    truncated — but it must stay a valid name.
    """
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.dashboards.enabled=true",
        "--set",
        f"fullnameOverride={fullname}",
    )

    assert result.returncode == 0, result.stderr
    configmaps = _dashboard_configmaps(result.stdout)
    assert len(configmaps) == len(_dashboard_files())
    names = [cm["metadata"]["name"] for cm in configmaps]
    assert len(names) == len(set(names)), f"colliding ConfigMap names: {sorted(names)}"
    for configmap in configmaps:
        name = configmap["metadata"]["name"]
        (key,) = configmap["data"]
        assert name.endswith(key.removesuffix(".json")), f"{name} lost the dashboard it holds"
        assert len(name) <= 253, f"{name} is {len(name)} characters"
        assert DNS_SUBDOMAIN.fullmatch(name), name


@requires_helm
def test_a_blank_sidecar_label_is_refused(tmp_path: Path):
    """It would render invalid YAML, and no label key means "watch everything"."""
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.dashboards.enabled=true",
        "--set",
        "monitoring.dashboards.label=",
    )

    assert result.returncode != 0
    assert "monitoring.dashboards.label" in result.stderr


@requires_helm
def test_platform_label_folder_and_namespace(tmp_path: Path):
    """The integrated case: the platform's sidecar decides the label, the
    annotation and the namespace it watches."""
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.dashboards.enabled=true",
        "--set",
        "monitoring.dashboards.label=platform_dashboard",
        "--set-string",
        "monitoring.dashboards.labelValue=yes",
        "--set",
        "monitoring.dashboards.labels.team=rag",
        "--set",
        "monitoring.dashboards.folderAnnotation=k8s-sidecar-target-directory",
        "--set",
        "monitoring.dashboards.folder=/tmp/dashboards/openrag",
        "--set",
        "monitoring.dashboards.namespace=grafana",
    )

    assert result.returncode == 0, result.stderr
    configmaps = _dashboard_configmaps(result.stdout)
    assert configmaps
    for configmap in configmaps:
        metadata = configmap["metadata"]
        assert metadata["labels"]["platform_dashboard"] == "yes"
        assert metadata["labels"]["team"] == "rag"
        assert "grafana_dashboard" not in metadata["labels"]
        assert metadata["annotations"] == {"k8s-sidecar-target-directory": "/tmp/dashboards/openrag"}
        assert metadata["namespace"] == "grafana"


@requires_helm
@pytest.mark.parametrize(
    "mode",
    [["--set", "monitoring.dashboards.enabled=true"], ["--set", "monitoring.bundled=true", *METRICS_TOKEN]],
    ids=["integrated", "bundled"],
)
def test_extra_labels_cannot_restate_the_watched_one(tmp_path: Path, mode: list[str]):
    """Rendered twice, the key lands with the last value: not the labelValue the
    bundled guard checks, and not one the sidecar watches."""
    result = _render(
        _isolated_chart(tmp_path), *mode, "--set-string", "monitoring.dashboards.labels.grafana_dashboard=0"
    )

    assert result.returncode != 0
    assert "monitoring.dashboards.labels sets grafana_dashboard" in result.stderr


@requires_helm
def test_no_folder_annotation_when_the_sidecar_has_none(tmp_path: Path):
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.dashboards.enabled=true",
        "--set",
        "monitoring.dashboards.folderAnnotation=",
    )

    assert result.returncode == 0, result.stderr
    configmaps = _dashboard_configmaps(result.stdout)
    assert configmaps
    assert all("annotations" not in cm["metadata"] for cm in configmaps)


@requires_helm
def test_bundled_ships_the_dashboards_and_scrapes_the_api_with_the_token(tmp_path: Path):
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true", *METRICS_TOKEN)

    assert result.returncode == 0, result.stderr
    assert len(_dashboard_configmaps(result.stdout)) == len(_dashboard_files())
    (monitor,) = _objects(result.stdout, "ServiceMonitor")
    (endpoint,) = monitor["spec"]["endpoints"]
    assert endpoint["path"] == "/metrics"
    assert endpoint["authorization"] == {
        "type": "Bearer",
        "credentials": {"name": "openrag-env-secrets", "key": "METRICS_TOKEN"},
    }


@requires_helm
@pytest.mark.parametrize(
    ("override", "names"),
    [
        (["--set", "monitoring.dashboards.label=platform_dashboard"], "label and labelValue"),
        (["--set-string", "monitoring.dashboards.labelValue=yes"], "label and labelValue"),
        (["--set", "monitoring.dashboards.folderAnnotation=k8s-folder"], "sidecar.dashboards.folderAnnotation"),
        (
            ["--set", "kubePrometheusStack.grafana.sidecar.dashboards.provider.foldersFromFilesStructure=false"],
            "foldersFromFilesStructure",
        ),
    ],
)
def test_bundled_refuses_what_its_grafana_would_not_load(tmp_path: Path, override: list[str], names: str):
    """The sidecar's settings are the sub-chart's, not derived from these: one
    changed alone leaves the dashboards unloaded, or outside their folder."""
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true", *METRICS_TOKEN, *override)

    assert result.returncode != 0
    assert names in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "override",
    [
        # Changed on both sides.
        [
            "--set",
            "monitoring.dashboards.label=platform_dashboard",
            "--set",
            "kubePrometheusStack.grafana.sidecar.dashboards.label=platform_dashboard",
            "--set",
            "monitoring.dashboards.folderAnnotation=k8s-folder",
            "--set",
            "kubePrometheusStack.grafana.sidecar.dashboards.folderAnnotation=k8s-folder",
        ],
        # An empty labelValue: the sidecar matches the label with any value.
        [
            "--set-string",
            "monitoring.dashboards.labelValue=yes",
            "--set-string",
            "kubePrometheusStack.grafana.sidecar.dashboards.labelValue=",
        ],
        # No folder annotation: the dashboards go to the sidecar's default folder.
        ["--set", "monitoring.dashboards.folderAnnotation="],
        # Another Grafana loads them; the bundled one's settings do not apply.
        [
            "--set",
            "kubePrometheusStack.grafana.enabled=false",
            "--set",
            "monitoring.dashboards.label=platform_dashboard",
        ],
    ],
)
def test_bundled_accepts_what_its_grafana_loads(tmp_path: Path, override: list[str]):
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true", *METRICS_TOKEN, *override)

    assert result.returncode == 0, result.stderr
    assert len(_dashboard_configmaps(result.stdout)) == len(_dashboard_files())


@requires_helm
def test_bundled_refuses_to_scrape_without_the_token(tmp_path: Path):
    """GET /metrics fails closed: without the token every scrape is a 403, which
    reads as a target that is merely down. The Compose overlay refuses too."""
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true")

    assert result.returncode != 0
    assert "env.secrets.METRICS_TOKEN" in result.stderr


@requires_helm
def test_bundled_trusts_an_external_secret_for_the_token(tmp_path: Path):
    """The chart cannot see into a Secret it does not render."""
    result = _render(
        _isolated_chart(tmp_path), "--set", "monitoring.bundled=true", "--set", "env.existingSecret=openrag-prod"
    )

    assert result.returncode == 0, result.stderr
    (monitor,) = _objects(result.stdout, "ServiceMonitor")
    assert monitor["spec"]["endpoints"][0]["authorization"]["credentials"]["name"] == "openrag-prod"


@requires_helm
def test_bundled_under_ray_serve_renders_no_api_monitor(tmp_path: Path):
    """Ray Serve replicas have no single target; bundling must not fail the
    install over a monitor it was not asked for."""
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true", *METRICS_TOKEN, *RAY_SERVE)

    assert result.returncode == 0, result.stderr
    assert not _objects(result.stdout, "ServiceMonitor")
    assert _dashboard_configmaps(result.stdout)


@requires_helm
def test_an_explicit_api_monitor_under_ray_serve_still_fails(tmp_path: Path):
    result = _render(_isolated_chart(tmp_path), "--set", "openrag.metrics.serviceMonitor.enabled=true", *RAY_SERVE)

    assert result.returncode != 0
    assert "not supported with ray.enabled=true and ENABLE_RAY_SERVE=true" in result.stderr


def _bundled_notes(tmp_path: Path, *args: str) -> str:
    """`helm template` renders NOTES.txt, so a template error in it fails every
    test above — but it never prints it, and `helm install --dry-run` needs a
    cluster, so what the notes actually say is otherwise asserted nowhere.
    Rendering the file under a second name is what puts the text in reach.
    """
    chart = _isolated_chart(tmp_path)
    notes = (chart / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    # Helm parses what it renders, and the notes are prose: indenting them under
    # a block scalar makes the output a YAML document without touching the text.
    (chart / "templates" / "notes-under-test.yaml").write_text("notes: |\n" + indent(notes, "  "), encoding="utf-8")

    result = _render(
        chart, "--set", "monitoring.bundled=true", *METRICS_TOKEN, *args, "-s", "templates/notes-under-test.yaml"
    )

    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)["notes"]


@requires_helm
def test_the_install_notes_tell_an_operator_how_to_open_grafana(tmp_path: Path):
    notes = _bundled_notes(tmp_path)

    assert "port-forward svc/openrag-grafana 3000:80" in notes
    assert "secret openrag-grafana -o jsonpath='{.data.admin-user}'" in notes
    assert "secret openrag-grafana -o jsonpath='{.data.admin-password}'" in notes
    assert 'the OpenRAG dashboards in the "OpenRAG" folder' in notes
    assert "Alertmanager ships with no receiver" in notes


@requires_helm
def test_the_install_notes_read_the_admin_from_an_existing_secret(tmp_path: Path):
    """Grafana then generates no Secret of its own: the one it reads is the one to print."""
    notes = _bundled_notes(
        tmp_path,
        "--set",
        "kubePrometheusStack.grafana.admin.existingSecret=grafana-admin",
        "--set",
        "kubePrometheusStack.grafana.admin.userKey=user",
        "--set",
        "kubePrometheusStack.grafana.admin.passwordKey=password",
    )

    assert "secret grafana-admin -o jsonpath='{.data.user}'" in notes
    assert "secret grafana-admin -o jsonpath='{.data.password}'" in notes
    assert "secret openrag-grafana" not in notes


@requires_helm
def test_the_install_notes_skip_what_is_not_bundled(tmp_path: Path):
    """Commands for a Service and a Secret nobody created read as a broken install."""
    notes = _bundled_notes(
        tmp_path,
        "--set",
        "kubePrometheusStack.grafana.enabled=false",
        "--set",
        "kubePrometheusStack.alertmanager.enabled=false",
    )

    assert "port-forward" not in notes
    assert "get secret" not in notes
    assert "Alertmanager" not in notes
    assert 'grafana_dashboard="1"' in notes


def _webhook_policy(rendered: str) -> dict | None:
    policies = [p for p in _objects(rendered, "NetworkPolicy") if p["metadata"]["name"].endswith("-monitoring-webhook")]
    return policies[0] if policies else None


@requires_helm
def test_bundled_lets_the_api_server_reach_the_admission_webhook(tmp_path: Path):
    """The default-deny policy admits this namespace and the public ports only.
    The webhook's caller is the API server, which no selector can name, so
    off-node its call is dropped: validation silently skipped under the default
    failurePolicy, every rule write rejected under Fail.
    """
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.bundled=true", *METRICS_TOKEN)

    assert result.returncode == 0, result.stderr
    policy = _webhook_policy(result.stdout)
    assert policy, "bundled renders no webhook policy"
    # The webhook's own pod, not the operator: the operator's listener also
    # serves /debug/pprof/, and this port is open to any source.
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "kube-prometheus-stack-operator-webhook"}}
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"port": 10250, "protocol": "TCP"}]
    # No `from`: the caller cannot be selected, so the port is open to any source.
    assert "from" not in rule


def test_bundled_serves_the_webhook_from_its_own_pod():
    """Upstream serves it from the operator, on the listener carrying /debug/pprof/."""
    operator = _values()["kubePrometheusStack"]["prometheusOperator"]

    assert operator["admissionWebhooks"]["deployment"]["enabled"] is True


@requires_helm
def test_without_its_own_pod_the_opening_moves_to_the_operator(tmp_path: Path):
    """Turned off, the operator serves the webhook, so that is the pod to open."""
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.bundled=true",
        *METRICS_TOKEN,
        "--set",
        "kubePrometheusStack.prometheusOperator.admissionWebhooks.deployment.enabled=false",
        "--set",
        "kubePrometheusStack.prometheusOperator.tls.internalPort=8443",
    )

    assert result.returncode == 0, result.stderr
    policy = _webhook_policy(result.stdout)
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "kube-prometheus-stack-operator"}}
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"port": 8443, "protocol": "TCP"}]


@requires_helm
@pytest.mark.parametrize(
    "disabled",
    ["prometheusOperator.admissionWebhooks.enabled", "prometheusOperator.enabled"],
)
def test_no_webhook_opening_without_a_webhook(tmp_path: Path, disabled: str):
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.bundled=true",
        *METRICS_TOKEN,
        "--set",
        f"kubePrometheusStack.{disabled}=false",
    )

    assert result.returncode == 0, result.stderr
    assert _webhook_policy(result.stdout) is None


@requires_helm
def test_the_webhook_opening_can_be_narrowed(tmp_path: Path):
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.bundled=true",
        *METRICS_TOKEN,
        "--set",
        "networkPolicy.webhookFrom[0].ipBlock.cidr=10.0.0.0/24",
    )

    assert result.returncode == 0, result.stderr
    (rule,) = _webhook_policy(result.stdout)["spec"]["ingress"]
    assert rule["from"] == [{"ipBlock": {"cidr": "10.0.0.0/24"}}]


@requires_helm
def test_the_webhook_opening_follows_the_webhook_port(tmp_path: Path):
    """Selecting the wrong port opens nothing, and looks like it opened something.
    The operator's own port is not the webhook's once the webhook has its pod."""
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.bundled=true",
        *METRICS_TOKEN,
        "--set",
        "kubePrometheusStack.prometheusOperator.admissionWebhooks.deployment.tls.internalPort=8443",
        "--set",
        "kubePrometheusStack.prometheusOperator.tls.internalPort=9443",
    )

    assert result.returncode == 0, result.stderr
    (rule,) = _webhook_policy(result.stdout)["spec"]["ingress"]
    assert rule["ports"] == [{"port": 8443, "protocol": "TCP"}]


@requires_helm
def test_no_webhook_opening_without_the_bundled_stack(tmp_path: Path):
    """Nothing in an integrated install serves that port in this namespace."""
    result = _render(_isolated_chart(tmp_path), "--set", "monitoring.dashboards.enabled=true")

    assert result.returncode == 0, result.stderr
    assert _webhook_policy(result.stdout) is None
    assert _objects(result.stdout, "NetworkPolicy"), "the default-deny policy is still expected"


@requires_helm
def test_no_webhook_opening_when_policies_are_off(tmp_path: Path):
    result = _render(
        _isolated_chart(tmp_path),
        "--set",
        "monitoring.bundled=true",
        *METRICS_TOKEN,
        "--set",
        "networkPolicy.enabled=false",
    )

    assert result.returncode == 0, result.stderr
    assert not _objects(result.stdout, "NetworkPolicy")


@requires_helm
def test_explicit_monitor_keeps_its_own_bearer_switch(tmp_path: Path):
    """Outside bundled mode the bearer stays opt-in, as before."""
    result = _render(_isolated_chart(tmp_path), "--set", "openrag.metrics.serviceMonitor.enabled=true")

    assert result.returncode == 0, result.stderr
    (monitor,) = _objects(result.stdout, "ServiceMonitor")
    assert "authorization" not in monitor["spec"]["endpoints"][0]


# The tests above read values.yaml, or render with the sub-charts stripped, so
# an upstream key renamed by a version bump would leave them passing while the
# setting it carried silently stops applying. These render the pinned
# kube-prometheus-stack itself. Its archive is fetched, not committed: they run
# wherever `helm dependency build` has run, as it does for a version bump, and
# skip elsewhere.
def _pinned_kube_prometheus_stack() -> Path:
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    dependency = next(d for d in chart["dependencies"] if d["name"] == "kube-prometheus-stack")
    return CHART_DIR / "charts" / f"kube-prometheus-stack-{dependency['version']}.tgz"


KUBE_PROMETHEUS_STACK = _pinned_kube_prometheus_stack()
requires_kube_prometheus_stack = pytest.mark.skipif(
    HELM is None or not KUBE_PROMETHEUS_STACK.exists(),
    reason=f"needs Helm and {KUBE_PROMETHEUS_STACK.name} (helm dependency build)",
)


@pytest.fixture(scope="module")
def bundled_stack(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    chart = _isolated_chart(tmp_path_factory.mktemp("bundled"))
    meta = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    meta["dependencies"] = [d for d in meta["dependencies"] if d["name"] == "kube-prometheus-stack"]
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (chart / "charts").mkdir()
    shutil.copy(KUBE_PROMETHEUS_STACK, chart / "charts")

    result = _render(chart, "--set", "monitoring.bundled=true", *METRICS_TOKEN)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _bundled_resources(objects: list[dict]):
    """(where, resources) for every container the sub-chart runs, hook Jobs
    included. Prometheus and Alertmanager are sized on their custom resource."""
    for obj in objects:
        name = f"{obj['kind']}/{obj['metadata']['name']}"
        if (obj["metadata"].get("labels") or {}).get("helm.sh/chart", "").startswith("openrag-stack-"):
            continue
        if obj["kind"] in ("Prometheus", "Alertmanager"):
            yield name, obj["spec"].get("resources") or {}
        elif obj["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            pod = obj["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                yield f"{name}/{container['name']}", container.get("resources") or {}


@requires_kube_prometheus_stack
def test_every_pod_the_bundled_stack_runs_reserves_what_it_uses(bundled_stack: list[dict]):
    """A pod without requests is BestEffort, and a namespace whose ResourceQuota
    requires them refuses it: for a hook Job, that fails the install."""
    sized = dict(_bundled_resources(bundled_stack))

    assert any(where.startswith("Job/") for where in sized), "no hook Job rendered"
    for where, resources in sized.items():
        assert (resources.get("requests") or {}).keys() == {"cpu", "memory"}, where
        assert (resources.get("limits") or {}).keys() == {"memory"}, where

    # The operator injects the config-reloader sidecar, sized by these flags; 0 is none.
    (operator,) = [
        o for o in bundled_stack if o["kind"] == "Deployment" and o["metadata"]["name"].endswith("-operator")
    ]
    flags = dict(
        arg.split("=", 1) for arg in operator["spec"]["template"]["spec"]["containers"][0]["args"] if "=" in arg
    )
    assert flags["--config-reloader-cpu-limit"] == "0"
    for flag in ("--config-reloader-cpu-request", "--config-reloader-memory-request", "--config-reloader-memory-limit"):
        assert flags[flag] != "0", flag


@requires_kube_prometheus_stack
def test_the_bundled_stack_watches_no_control_plane(bundled_stack: list[dict]):
    """On a managed control plane these Services select pods that do not exist,
    and the absent(up) alerts on them fire forever."""
    system_services = [
        o["metadata"]["name"]
        for o in bundled_stack
        if o["kind"] == "Service" and o["metadata"].get("namespace") == "kube-system"
    ]
    alerts = {
        rule["alert"]
        for o in bundled_stack
        if o["kind"] == "PrometheusRule"
        for group in o["spec"]["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }

    assert "KubeletDown" in alerts, "no upstream rules rendered"
    assert not [s for s in system_services if re.search(r"controller-manager|scheduler|etcd|proxy", s)]
    assert not alerts & {"KubeControllerManagerDown", "KubeSchedulerDown", "KubeProxyDown", "etcdMembersDown"}
