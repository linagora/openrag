"""Inference images and the default embedder, across the chart and Compose (#1031).

Compose still runs vLLM v0.9.2, where ``--task`` is valid, so the vLLM checks
here cover the chart only; Compose joins them when it moves to the chart's
version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.config.endpoints import EmbedderConfig

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
VALUES = CHART_DIR / "values.yaml"
VALUES_FILES = sorted(CHART_DIR.glob("values*.yaml"))
COMPOSE = ROOT / "infra" / "compose" / "docker-compose.yaml"
ENV_EXAMPLE = ROOT / "infra" / "compose" / ".env.example"
COMPOSE_INFINITY = ROOT / "extern" / "reranker" / "infinity.yaml"
CONFIG = ROOT / "conf" / "config.yaml"

# Not built by any workflow yet, so there is no version to pin it to (#1031).
UNPINNED_UNTIL_BUILT = {"ghcr.io/linagora/vllm-whisper"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _engines(path: Path) -> list[dict]:
    return _load(path).get("vllm", {}).get("servingEngineSpec", {}).get("modelSpec", [])


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_chart_inference_images_have_an_explicit_version(path: Path):
    images = [(spec["repository"], spec.get("tag")) for spec in _engines(path)]
    reranker = _load(path).get("reranker", {}).get("image")
    if reranker:
        images.append((reranker["repository"], reranker.get("tag")))

    for repository, tag in images:
        if repository in UNPINNED_UNTIL_BUILT:
            continue
        assert tag and not str(tag).startswith("latest"), f"{path.name}: {repository}:{tag}"


@pytest.mark.parametrize("path", VALUES_FILES, ids=lambda p: p.name)
def test_chart_vllm_engines_share_one_version(path: Path):
    """vlm is left out: it stays on the version it was validated with."""
    tags = {
        spec["tag"] for spec in _engines(path) if spec["repository"] == "vllm/vllm-openai" and spec["name"] != "vlm"
    }
    assert len(tags) <= 1, f"{path.name}: {sorted(tags)}"


def test_compose_runs_the_chart_infinity_version():
    tag = _load(VALUES)["reranker"]["image"]["tag"]
    services = _load(COMPOSE_INFINITY)["services"]

    assert services["reranker"]["image"] == f"michaelf34/infinity:{tag}"
    assert services["reranker-cpu"]["image"] == f"michaelf34/infinity:{tag}-cpu"


def _env_example_value(name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} not set in .env.example"
    return match.group(1).strip()


def test_every_install_mode_defaults_to_the_same_embedder():
    """A different default per install mode means a different vector space per
    install mode, and moving between them silently needs a reindex."""
    expected = _load(VALUES)["vllm"]["embedderModelName"]
    compose_defaults = re.findall(r"\$\{EMBEDDER_MODEL_NAME:-([^}]+)\}", COMPOSE.read_text(encoding="utf-8"))

    defaults = {
        "conf/config.yaml": _load(CONFIG)["embedder"]["model_name"],
        "EmbedderConfig": EmbedderConfig.model_fields["model_name"].default,
        ".env.example": _env_example_value("EMBEDDER_MODEL_NAME"),
        **{f"docker-compose.yaml #{i}": value for i, value in enumerate(compose_defaults)},
        **{
            f"{path.name} embedder": spec["modelURL"]
            for path in VALUES_FILES
            for spec in _engines(path)
            if spec["name"] == "embedder" and isinstance(spec.get("modelURL"), str)
        },
    }

    assert len(compose_defaults) == 2, "expected the vllm-gpu and vllm-cpu defaults"
    assert {name: value for name, value in defaults.items() if value != expected} == {}


def test_the_quick_start_env_file_is_the_compose_template():
    """The Quick Start page renders ``docs/assets/env_example.env`` as
    ``infra/compose/.env``. It is a copy, so it drifts: a new install that
    follows the docs got the previous default embedder while every other
    default had moved to Qwen."""
    docs_copy = ROOT / "docs" / "assets" / "env_example.env"
    assert docs_copy.read_bytes() == ENV_EXAMPLE.read_bytes(), (
        "run: cp infra/compose/.env.example docs/assets/env_example.env"
    )
