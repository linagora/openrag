"""Marker's OCR server in Compose (surya-vllm, surya-llamacpp)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "infra" / "compose" / "docker-compose.yaml"
MODEL = "datalab-to/surya-ocr-2.5"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _default(value: str) -> str:
    """What Compose substitutes for ``${NAME:-default}`` when NAME is unset."""
    return re.sub(r"\$\{[A-Z_]+:?-([^}]*)\}", r"\1", value)


def _command(service: dict) -> list[str]:
    return shlex.split(_default(service["command"]))


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


@pytest.mark.parametrize(("server", "api"), [("surya-vllm", "openrag"), ("surya-llamacpp", "openrag-cpu")])
def test_each_profile_pairs_the_api_with_its_ocr_server(server, api):
    services = _services()

    assert services[server]["profiles"] == services[api]["profiles"]
    assert services[server]["networks"]["default"]["aliases"] == ["surya-ocr"]
    assert services[server]["healthcheck"]["test"][-1] == "http://localhost:8000/health"
    assert _default(services[api]["environment"]["SURYA_INFERENCE_URL"]) == "http://surya-ocr:8000/v1"


def test_both_servers_answer_as_the_model_surya_checks():
    """surya refuses a server that reports another model name."""
    services = _services()

    assert _flag(_command(services["surya-vllm"]), "--served-model-name") == MODEL
    llamacpp = _command(services["surya-llamacpp"])
    assert (_flag(llamacpp, "--alias"), _flag(llamacpp, "--port")) == (MODEL, "8000")


@pytest.mark.parametrize(
    ("server", "flag", "api", "expected"),
    [("surya-vllm", "--max-num-seqs", "openrag", "32"), ("surya-llamacpp", "--parallel", "openrag-cpu", "4")],
)
def test_the_api_splits_the_requests_the_server_takes(server, flag, api, expected):
    """One variable sizes both sides: the server's request cap and the budget
    Marker splits across its slots."""
    services = _services()

    assert re.search(r"\$\{MARKER_OCR_CONCURRENCY:-(\d+)\}", services[server]["command"])
    assert _flag(_command(services[server]), flag) == expected
    assert services[api]["environment"]["MARKER_OCR_CONCURRENCY"] == f"${{MARKER_OCR_CONCURRENCY:-{expected}}}"


def test_the_backends_match_their_servers():
    services = _services()

    assert _default(services["openrag"]["environment"]["SURYA_INFERENCE_BACKEND"]) == "vllm"
    assert _default(services["openrag-cpu"]["environment"]["SURYA_INFERENCE_BACKEND"]) == "llamacpp"


def test_cpu_runs_marker_without_ocr_unless_asked():
    """OCR on CPU is too slow for every page; the GPU API keeps the config default."""
    services = _services()

    assert _default(services["openrag-cpu"]["environment"]["MARKER_MODE"]) == "fast_no_ocr"
    assert "MARKER_MODE" not in services["openrag"]["environment"]


def test_the_llamacpp_image_is_pinned():
    image = _services()["surya-llamacpp"]["image"]

    assert re.fullmatch(r"ghcr\.io/ggml-org/llama\.cpp:server-b\d+", image), image
