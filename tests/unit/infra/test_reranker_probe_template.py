from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

DEFAULT_MODEL = "Alibaba-NLP/gte-multilingual-reranker-base"
# Infinity 0.0.77 on CPU, cached model, measured to the first /health answer.
SLOWEST_MEASURED_START_SECONDS = 341
# The reranker block as values files copied from chart 0.7.1 or earlier carry
# it: the keys this chart removed, at the values they had.
COPIED_LEGACY_VALUES = f"""
reranker:
  rerankerModelName: {DEFAULT_MODEL}
  servicePort: 7997
  command: [v2, --model-id, {DEFAULT_MODEL}, --port, 7997]
env:
  config:
    RERANKER_MODEL: '{{{{ .Values.reranker.rerankerModelName }}}}'
    RERANKER_BASE_URL: 'http://{{{{ include "openrag-stack.fullname" . }}}}-reranker:{{{{ .Values.reranker.servicePort }}}}'
"""


def _isolated_chart(tmp_path: Path) -> Path:
    """Copy only the parent-chart files needed to render the reranker and its env."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "infinity.yaml", "configmap-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n",
        encoding="utf-8",
    )
    return chart


def _render(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(_isolated_chart(tmp_path)), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _documents(tmp_path: Path, *args: str) -> list[dict]:
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _reranker_container(documents: list[dict]) -> dict:
    deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-reranker"))
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _env_config(documents: list[dict]) -> dict:
    return next(d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-env"))["data"]


def _budget_seconds(probe: dict) -> int:
    return probe.get("initialDelaySeconds", 0) + probe.get("periodSeconds", 10) * probe.get("failureThreshold", 3)


# Probes


@requires_helm
def test_a_slow_start_is_covered_by_the_startup_probe(tmp_path: Path) -> None:
    """Liveness used to kill Infinity before its warmup ended; it now waits for the startup probe."""
    container = _reranker_container(_documents(tmp_path))

    for kind in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[kind]["httpGet"] == {"path": "/health", "port": "reranker"}
    assert _budget_seconds(container["startupProbe"]) > SLOWEST_MEASURED_START_SECONDS


@requires_helm
def test_probe_values_are_rendered(tmp_path: Path) -> None:
    container = _reranker_container(
        _documents(
            tmp_path,
            "--set",
            "reranker.probes.startup.failureThreshold=120",
            "--set",
            "reranker.probes.readiness.timeoutSeconds=5",
            "--set",
            "reranker.probes.liveness.periodSeconds=15",
        )
    )

    assert container["startupProbe"]["failureThreshold"] == 120
    assert container["readinessProbe"]["timeoutSeconds"] == 5
    assert container["livenessProbe"]["periodSeconds"] == 15
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"


@requires_helm
def test_a_null_probe_is_dropped(tmp_path: Path) -> None:
    container = _reranker_container(_documents(tmp_path, "--set", "reranker.probes.startup=null"))

    assert "startupProbe" not in container
    assert "livenessProbe" in container


@requires_helm
def test_a_probe_takes_another_handler_once_http_get_is_null(tmp_path: Path) -> None:
    container = _reranker_container(
        _documents(
            tmp_path,
            "--set",
            "reranker.probes.liveness.httpGet=null",
            "--set",
            "reranker.probes.liveness.tcpSocket.port=reranker",
        )
    )

    assert container["livenessProbe"]["tcpSocket"] == {"port": "reranker"}
    assert "httpGet" not in container["livenessProbe"]


@requires_helm
def test_no_probes_block_renders_without_probes(tmp_path: Path) -> None:
    container = _reranker_container(_documents(tmp_path, "--set", "reranker.probes=null"))

    assert not {"startupProbe", "readinessProbe", "livenessProbe"} & container.keys()


# One source for the model and the port


@requires_helm
def test_model_id_is_the_model_infinity_serves_and_openrag_names(tmp_path: Path) -> None:
    documents = _documents(tmp_path, "--set", "reranker.model.id=BAAI/bge-reranker-v2-m3")

    assert _reranker_container(documents)["args"][:3] == ["v2", "--model-id", "BAAI/bge-reranker-v2-m3"]
    assert _env_config(documents)["RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"


@requires_helm
def test_service_port_is_the_port_infinity_listens_on_and_openrag_calls(tmp_path: Path) -> None:
    documents = _documents(tmp_path, "--set", "reranker.service.port=8000")

    container = _reranker_container(documents)
    assert container["args"][3:5] == ["--port", "8000"]
    assert container["ports"] == [{"containerPort": 8000, "name": "reranker"}]
    service = next(d for d in documents if d["kind"] == "Service" and d["metadata"]["name"].endswith("-reranker"))
    assert service["spec"]["ports"][0]["port"] == 8000
    assert _env_config(documents)["RERANKER_BASE_URL"] == "http://openrag-reranker:8000"


@requires_helm
def test_a_values_file_copied_from_the_previous_chart_still_renders(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(COPIED_LEGACY_VALUES, encoding="utf-8")

    documents = _documents(tmp_path, "-f", str(legacy))

    assert _reranker_container(documents)["args"] == ["v2", "--model-id", DEFAULT_MODEL, "--port", "7997"]
    assert _env_config(documents)["RERANKER_MODEL"] == DEFAULT_MODEL
    assert _env_config(documents)["RERANKER_BASE_URL"] == "http://openrag-reranker:7997"


@requires_helm
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("reranker.rerankerModelName=BAAI/bge-reranker-v2-m3", "env.config.RERANKER_MODEL renders"),
        ("reranker.model.id=BAAI/bge-reranker-v2-m3", "env.config.RERANKER_MODEL renders"),
        ("reranker.servicePort=8000", "env.config.RERANKER_BASE_URL renders"),
        ("reranker.service.port=8000", "env.config.RERANKER_BASE_URL renders"),
    ],
)
def test_a_copied_values_file_changing_one_of_two_keys_is_refused(tmp_path: Path, override: str, message: str) -> None:
    """Before, these rendered: the model change reranked with another model than
    the one OpenRAG named, the port change left OpenRAG calling a closed port."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(COPIED_LEGACY_VALUES, encoding="utf-8")

    result = _render(tmp_path, "-f", str(legacy), "--set", override)

    assert result.returncode != 0
    assert message in result.stderr


@requires_helm
def test_command_flags_that_never_applied_are_refused(tmp_path: Path) -> None:
    result = _render(tmp_path, "--set", f"reranker.command={{v2,--model-id,{DEFAULT_MODEL},--port,7997,--batch-size,8}}")

    assert result.returncode != 0
    assert "reranker.command is not read by the chart" in result.stderr


@requires_helm
def test_a_service_dns_name_with_the_right_port_is_accepted(tmp_path: Path) -> None:
    url = "http://openrag-reranker.openrag.svc.cluster.local:7997"
    documents = _documents(tmp_path, "--set-string", f"env.config.RERANKER_BASE_URL={url}")

    assert _env_config(documents)["RERANKER_BASE_URL"] == url


@requires_helm
def test_a_url_without_a_port_is_checked_against_its_scheme_default(tmp_path: Path) -> None:
    url = "env.config.RERANKER_BASE_URL=http://openrag-reranker"

    assert _render(tmp_path, "--set-string", url).returncode != 0
    assert _render(tmp_path, "--set-string", url, "--set", "reranker.service.port=80").returncode == 0


@requires_helm
def test_an_external_reranker_is_not_checked_against_the_bundled_one(tmp_path: Path) -> None:
    documents = _documents(
        tmp_path,
        "--set",
        "reranker.externalUrl=http://reranker.example:8000",
        "--set-string",
        "env.config.RERANKER_MODEL=BAAI/bge-reranker-v2-m3",
    )

    assert _env_config(documents)["RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"


# Flags and environment of its own


@requires_helm
def test_extra_args_and_env_reach_the_reranker(tmp_path: Path) -> None:
    container = _reranker_container(
        _documents(
            tmp_path,
            "--set",
            "reranker.extraArgs={--batch-size,16}",
            "--set",
            "reranker.extraEnv[0].name=INFINITY_MODEL_WARMUP",
            "--set-string",
            "reranker.extraEnv[0].value=false",
        )
    )

    assert container["args"] == ["v2", "--model-id", DEFAULT_MODEL, "--port", "7997", "--batch-size", "16"]
    assert container["env"] == [{"name": "INFINITY_MODEL_WARMUP", "value": "false"}]
    assert container["envFrom"] == [{"configMapRef": {"name": "openrag-env"}}]
