from __future__ import annotations

from types import SimpleNamespace

from services.workers.parsers import docling_workers


def _config(docling_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Docling GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(docling_num_gpus=docling_num_gpus))


def test_docling_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Docling must request a GPU from Ray even when local CUDA is hidden in the
    pool process (CUDA_VISIBLE_DEVICES=""), otherwise it silently parses on CPU."""
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config()) == 0.25


def test_docling_num_gpus_zero_when_cluster_has_no_gpu(monkeypatch):
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", dict)

    assert docling_workers._docling_num_gpus(_config()) == 0


def test_docling_num_gpus_zero_when_not_requested(monkeypatch):
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config(docling_num_gpus=0)) == 0


# --- #722: the detached pool supervisor must survive its own crashes ----------


def test_docling_pool_sets_max_restarts():
    """DoclingPool had a bare @ray.remote — Ray's default max_restarts is 0, so
    one crash left the detached actor permanently dead and every docling parse
    afterwards failed. It must be restartable like MarkerPool/WhisperActor."""
    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == 5


def test_docling_pool_restart_policy_matches_marker():
    """Parity guard: if MarkerPool's policy changes, this flags that DoclingPool
    should be reconsidered too, rather than silently drifting apart."""
    from services.workers.parsers.marker_workers import MarkerPool

    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == MarkerPool.__ray_metadata__.max_restarts
