"""The vLLM engines the chart ships through vllm-stack.

Every values file that restates ``vllm.servingEngineSpec.modelSpec`` is checked:
Helm replaces lists wholesale, so an overlay's engines are the ones that run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
VALUES = CHART_DIR / "values.yaml"
VALUES_FILES = sorted(CHART_DIR.glob("values*.yaml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _engines(path: Path) -> list[dict]:
    return _load(path).get("vllm", {}).get("servingEngineSpec", {}).get("modelSpec", [])


def _engine(name: str) -> dict:
    return next(spec for spec in _engines(VALUES) if spec["name"] == name)


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_vllm_openai_engines_are_pinned(path: Path):
    """``latest`` let a pod restart pull whatever vLLM was newest, which is how
    the embedder stopped starting when vLLM dropped ``--task`` (#1031)."""
    engines = [spec for spec in _engines(path) if spec["repository"] == "vllm/vllm-openai"]
    for spec in engines:
        assert spec["tag"] != "latest", f"{path.name}: {spec['name']} runs vllm/vllm-openai:latest"


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_vllm_openai_engines_run_the_cuda_12_build(path: Path):
    """``vllm/vllm-openai:v0.30.0`` is the CUDA 13 build and needs an NVIDIA
    driver >= 580: on an older one the embedder fails CUDA init, and indexing and
    search go down, while vlm (CUDA 12.9) starts. ``-cu129`` keeps every engine on
    the CUDA vlm already needs."""
    engines = [spec for spec in _engines(path) if spec["repository"] == "vllm/vllm-openai"]
    for spec in engines:
        if str(spec["tag"]).startswith("v0.30"):
            assert str(spec["tag"]).endswith("-cu129"), f"{path.name}: {spec['name']} runs {spec['tag']}"


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_no_engine_passes_task(path: Path):
    """vLLM v0.30.0 exits on ``--task`` (``unrecognized arguments``)."""
    for spec in _engines(path):
        args = (spec.get("vllmConfig") or {}).get("extraArgs", [])
        assert not any(arg.startswith("--task") for arg in args), f"{path.name}: {spec['name']} passes {args}"


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_embedder_runs_the_pooling_runner(path: Path):
    """Qwen3-Embedding declares a causal-LM architecture: without
    ``--runner pooling`` vLLM serves it as a generator, with no /v1/embeddings."""
    embedders = [spec for spec in _engines(path) if spec["name"] == "embedder"]
    for spec in embedders:
        assert spec["vllmConfig"].get("runner") == "pooling", path.name


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_embedder_converts_to_embeddings(path: Path):
    """``--task embed`` used to do both halves; v0.30.0 splits it into
    ``--runner pooling`` and ``--convert embed``. The pair is the command that
    was started on v0.30.0; the runner alone never was."""
    embedders = [spec for spec in _engines(path) if spec["name"] == "embedder"]
    for spec in embedders:
        args = spec["vllmConfig"].get("extraArgs", [])
        pairs = list(zip(args, args[1:]))
        assert ("--convert", "embed") in pairs, f"{path.name}: embedder passes {args}"


def test_engine_images_are_not_pulled_on_every_start():
    assert _load(VALUES)["vllm"]["servingEngineSpec"]["imagePullPolicy"] == "IfNotPresent"


def test_engines_have_a_writable_home_and_cache():
    """The engines run as uid 10001, which has no home directory in the images:
    with HOME=/ FlashInfer's JIT workspace and vLLM's cache fail with
    ``Permission denied: '/.cache'`` (#1036)."""
    spec = _load(VALUES)["vllm"]["servingEngineSpec"]
    assert spec["securityContext"]["runAsUser"] != 0
    env = {item["name"]: item["value"] for item in spec["env"]}
    assert env["HOME"].startswith("/tmp")
    assert env["XDG_CACHE_HOME"].startswith("/tmp")


def test_llm_endpoint_is_empty_while_the_bundled_llm_is_scaled_to_zero():
    """A URL to an engine with no pods is seeded as the default LLM endpoint on
    the first boot, and chat fails later with ``Connection error`` (#1040)."""
    config = _load(VALUES)["env"]["config"]
    if _engine("llm")["replicaCount"] == 0:
        assert config["BASE_URL"] == ""
        assert config["MODEL"] == ""


def test_vllm_stack_minimum_renders_the_engine_wide_env():
    """vllm-stack 0.1.7 appends ``modelSpec.env`` only and ignores
    ``servingEngineSpec.env``, so the writable HOME above (#1036) would silently
    disappear if dependencies ever resolved that low. 0.1.12 is the version
    checked to render it."""
    chart = yaml.safe_load((VALUES.parent / "Chart.yaml").read_text(encoding="utf-8"))
    (dep,) = [d for d in chart["dependencies"] if d["name"] == "vllm-stack"]
    minimum = dep["version"].split()[0].removeprefix(">=")
    assert tuple(int(x) for x in minimum.split(".")) >= (0, 1, 12), dep["version"]
