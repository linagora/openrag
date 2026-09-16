"""GPU panels must work on both deployments, which run different exporters.

Compose runs utkuozdemir/nvidia_gpu_exporter (``nvidia_smi_*``); Kubernetes gets
NVIDIA's DCGM exporter from the GPU Operator (``DCGM_FI_*``). Neither emits the
other's names, so a panel querying one namespace is permanently empty on the other
deployment (#932). Every GPU query therefore reads both, and these tests keep it so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

DASHBOARDS = Path(__file__).resolve().parents[3] / "infra/compose/grafana/dashboards"

# Scraped from the real exporters on an NVIDIA L4 (driver 580.95.05):
# dcgm-exporter 4.6.0-4.8.3 with the GPU Operator's collector list
# (dcp-metrics-included.csv), and nvidia_gpu_exporter 1.13.1 (utilization,
# memory, temperature and power families only). A name missing here has not been
# checked against an exporter — scrape one and add it rather than guess.
DCGM_METRICS = frozenset(
    """
    DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS DCGM_FI_DEV_DEC_UTIL DCGM_FI_DEV_ENC_UTIL
    DCGM_FI_DEV_FB_FREE DCGM_FI_DEV_FB_RESERVED DCGM_FI_DEV_FB_USED DCGM_FI_DEV_GPU_TEMP
    DCGM_FI_DEV_GPU_UTIL DCGM_FI_DEV_MEMORY_TEMP DCGM_FI_DEV_MEM_CLOCK
    DCGM_FI_DEV_MEM_COPY_UTIL DCGM_FI_DEV_PCIE_REPLAY_COUNTER DCGM_FI_DEV_POWER_USAGE
    DCGM_FI_DEV_ROW_REMAP_FAILURE DCGM_FI_DEV_SM_CLOCK DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION
    DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS DCGM_FI_DEV_VGPU_LICENSE_STATUS
    DCGM_FI_PROF_DRAM_ACTIVE DCGM_FI_PROF_GR_ENGINE_ACTIVE DCGM_FI_PROF_PCIE_RX_BYTES
    DCGM_FI_PROF_PCIE_TX_BYTES DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
    """.split()
)
NVIDIA_SMI_METRICS = frozenset(
    """
    nvidia_smi_memory_free_bytes nvidia_smi_memory_reserved_bytes
    nvidia_smi_memory_total_bytes nvidia_smi_memory_used_bytes
    nvidia_smi_power_draw_average_watts nvidia_smi_power_draw_instant_watts
    nvidia_smi_power_draw_watts nvidia_smi_power_limit_watts nvidia_smi_temperature_gpu
    nvidia_smi_utilization_decoder_ratio nvidia_smi_utilization_encoder_ratio
    nvidia_smi_utilization_gpu_ratio nvidia_smi_utilization_memory_ratio
    """.split()
)

DCGM_NAME = re.compile(r"\bDCGM_FI_[A-Z0-9_]+")
NVIDIA_SMI_NAME = re.compile(r"\bnvidia_smi_[a-z0-9_]+")


def _panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels", []))


def _gpu_queries() -> list[tuple[str, str]]:
    queries = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in _panels(dashboard.get("panels", [])):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if DCGM_NAME.search(expr) or NVIDIA_SMI_NAME.search(expr):
                    queries.append((f"{path.name}: {panel.get('title')}", expr))
    return queries


GPU_QUERIES = _gpu_queries()


def test_gpu_panels_are_found():
    # Guards the parametrized tests below against passing vacuously.
    assert GPU_QUERIES


@pytest.mark.parametrize(("panel", "expr"), GPU_QUERIES, ids=[p for p, _ in GPU_QUERIES])
def test_gpu_query_reads_both_exporters(panel: str, expr: str):
    assert DCGM_NAME.search(expr), f"{panel} is empty on Kubernetes (DCGM exporter)"
    assert NVIDIA_SMI_NAME.search(expr), f"{panel} is empty on Compose (nvidia_gpu_exporter)"
    assert re.search(r"\bor\b", expr), f"{panel} must fall back from one exporter to the other"


@pytest.mark.parametrize(("panel", "expr"), GPU_QUERIES, ids=[p for p, _ in GPU_QUERIES])
def test_gpu_query_uses_names_the_exporters_emit(panel: str, expr: str):
    assert set(DCGM_NAME.findall(expr)) <= DCGM_METRICS, panel
    assert set(NVIDIA_SMI_NAME.findall(expr)) <= NVIDIA_SMI_METRICS, panel


@pytest.mark.parametrize(("panel", "expr"), GPU_QUERIES, ids=[p for p, _ in GPU_QUERIES])
def test_dcgm_totals_count_each_gpu_once(panel: str, expr: str):
    """With KUBERNETES_VIRTUAL_GPUS on (time-slicing or MPS), the DCGM exporter
    repeats each GPU's series once per pod using it — and OpenRag's parser,
    embedder and reranker share one GPU. Summing those raw multiplies the card;
    collapse them per GPU (max by UUID) first.
    """
    naive = re.search(r"\b(?:sum|count)\s*(?:(?:by|without)\s*\([^)]*\)\s*)?\(\s*DCGM_FI_", expr)
    assert naive is None, f"{panel} totals raw DCGM series: {naive.group(0)!r}"


@pytest.mark.parametrize(("panel", "expr"), GPU_QUERIES, ids=[p for p, _ in GPU_QUERIES])
def test_dcgm_reserved_memory_is_optional(panel: str, expr: str):
    """DCGM_FI_DEV_FB_RESERVED only exists from dcgm-exporter 4.4.0 (GPU Operator
    v25.3.3); on older exporters an unguarded reference empties the whole panel.
    """
    for match in re.finditer(r"DCGM_FI_DEV_FB_RESERVED", expr):
        assert re.match(r"[)\s]*or\s+vector\(0\)", expr[match.end() :]), panel
