from __future__ import annotations

from types import SimpleNamespace

from services.workers.parsers import marker_workers


def _config(marker_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Marker GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(marker_num_gpus=marker_num_gpus))


def test_marker_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Marker should request GPUs from Ray even when local CUDA is hidden."""
    monkeypatch.setattr(marker_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(marker_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert marker_workers._marker_num_gpus(_config()) == 0.25
