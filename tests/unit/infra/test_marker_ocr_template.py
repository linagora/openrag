"""Marker's OCR server in the chart: markerOcr picks the server, and the env
ConfigMap tells OpenRAG where it is and how many requests it takes."""

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
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

ENV_KEYS = ("SURYA_INFERENCE_URL", "SURYA_INFERENCE_BACKEND", "MARKER_OCR_CONCURRENCY", "MARKER_MODE")


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Only the parent-chart files that render the OCR server and the env."""
    chart = tmp_path_factory.mktemp("chart") / "openrag-stack"
    (chart / "templates").mkdir(parents=True)
    for name in ("_helpers.tpl", "marker-ocr.yaml", "configmap-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, chart / "templates" / name)
    for name in ("values.yaml", "values-linagora.yaml"):
        shutil.copy(CHART_DIR / name, chart / name)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _render(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run([HELM, "template", "test", str(chart), *args], check=False, capture_output=True, text=True)


def _documents(chart: Path, *args: str) -> list[dict]:
    result = _render(chart, *args)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _env(documents: list[dict]) -> dict:
    data = next(d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-env"))["data"]
    return {key: data[key] for key in ENV_KEYS}


def _llamacpp(documents: list[dict], kind: str) -> dict | None:
    return next(
        (d for d in documents if d["kind"] == kind and d["metadata"]["name"].endswith("-surya-llamacpp")),
        None,
    )


def _engines_file(tmp_path: Path, **surya: object) -> str:
    """A values file restating the engine list with the surya engine changed:
    Helm replaces lists wholesale, so this is how a deployment changes one."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    engines = values["vllm"]["servingEngineSpec"]["modelSpec"]
    for engine in engines:
        if engine["name"] == "surya":
            engine.update(surya)
    path = tmp_path / "engines.yaml"
    path.write_text(yaml.safe_dump({"vllm": {"servingEngineSpec": {"modelSpec": engines}}}), encoding="utf-8")
    return str(path)


def test_by_default_marker_uses_the_bundled_vllm_engine(chart):
    documents = _documents(chart)

    assert _env(documents) == {
        "SURYA_INFERENCE_URL": "http://test-surya-engine-service/v1",
        "SURYA_INFERENCE_BACKEND": "vllm",
        "MARKER_OCR_CONCURRENCY": "32",
        "MARKER_MODE": "fast",
    }
    assert _llamacpp(documents, "Deployment") is None


def test_the_llamacpp_backend_runs_its_own_server(chart):
    documents = _documents(
        chart, "--set", "markerOcr.backend=llamacpp", "--set", "markerOcr.concurrency=4", "--set", "vllm.enabled=false"
    )

    assert _env(documents) == {
        "SURYA_INFERENCE_URL": "http://openrag-surya-llamacpp:8000/v1",
        "SURYA_INFERENCE_BACKEND": "llamacpp",
        "MARKER_OCR_CONCURRENCY": "4",
        "MARKER_MODE": "fast_no_ocr",
    }
    pod = _llamacpp(documents, "Deployment")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    args = container["args"]
    assert container["image"] == "ghcr.io/ggml-org/llama.cpp:server-b11681"
    # surya rejects a server that does not answer as its model's name.
    assert args[args.index("--alias") + 1] == "datalab-to/surya-ocr-2"
    assert args[args.index("--port") + 1] == "8000"
    assert args[args.index("--parallel") + 1] == "4"
    assert args[args.index("--threads") + 1] == "8"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert {"name": "LLAMA_CACHE", "value": "/models/llama.cpp"} in container["env"]
    service = _llamacpp(documents, "Service")
    assert [(p["port"], p["targetPort"]) for p in service["spec"]["ports"]] == [(8000, "http")]


def test_without_an_ocr_server_marker_runs_without_ocr(chart):
    documents = _documents(chart, "--set", "markerOcr.backend=none")

    env = _env(documents)
    assert (env["SURYA_INFERENCE_URL"], env["MARKER_MODE"]) == ("", "fast_no_ocr")
    assert _llamacpp(documents, "Deployment") is None


def test_an_external_server_needs_no_bundled_one(chart):
    documents = _documents(
        chart, "--set", "markerOcr.externalUrl=http://ocr.example:8000/v1", "--set", "vllm.enabled=false"
    )

    env = _env(documents)
    assert (env["SURYA_INFERENCE_URL"], env["SURYA_INFERENCE_BACKEND"]) == ("http://ocr.example:8000/v1", "vllm")
    assert _llamacpp(documents, "Deployment") is None


def test_the_linagora_overlay_runs_marker_without_ocr(chart):
    env = _env(_documents(chart, "-f", str(chart / "values-linagora.yaml")))

    assert (env["SURYA_INFERENCE_URL"], env["MARKER_MODE"]) == ("", "fast_no_ocr")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--set", "markerOcr.backend=tesseract"], 'markerOcr.backend is "tesseract"'),
        (["--set", "markerOcr.concurrency=16"], "serves 32 requests at once (vllmConfig.maxNumSeqs)"),
        (["--set", "vllm.enabled=false"], 'no "surya" engine runs'),
        (
            ["--set", "markerOcr.backend=none", "--set", "markerOcr.externalUrl=http://ocr.example/v1"],
            "markerOcr.backend is none",
        ),
        (
            ["--set", "markerOcr.backend=llamacpp", "--set", "markerOcr.llamacpp.extraArgs={--parallel=2}"],
            "markerOcr.llamacpp.extraArgs sets --parallel",
        ),
        (
            ["--set", "markerOcr.backend=llamacpp", "--set", "markerOcr.llamacpp.extraArgs={--alias,other}"],
            "markerOcr.llamacpp.extraArgs sets --alias",
        ),
    ],
    ids=["unknown-backend", "budget-mismatch", "no-engine", "none-with-url", "parallel-twice", "alias-twice"],
)
def test_a_server_marker_cannot_use_is_refused(chart, args, message):
    result = _render(chart, *args)

    assert result.returncode != 0
    assert message in result.stderr


def test_a_scaled_down_engine_is_refused_while_marker_points_at_it(chart, tmp_path):
    result = _render(chart, "-f", _engines_file(tmp_path, replicaCount=0))

    assert result.returncode != 0
    assert "the surya engine has replicaCount 0" in result.stderr


def test_the_engine_and_the_budget_move_together(chart, tmp_path):
    engines = _engines_file(tmp_path, vllmConfig={"maxNumSeqs": 64, "maxModelLen": 18000})

    env = _env(_documents(chart, "-f", engines, "--set", "markerOcr.concurrency=64"))

    assert env["MARKER_OCR_CONCURRENCY"] == "64"
