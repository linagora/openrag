from __future__ import annotations

from types import SimpleNamespace

from services.workers.parsers import whisper_workers


def _config(whisper_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Whisper GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(local_whisper=SimpleNamespace(whisper_num_gpus=whisper_num_gpus)))


def test_whisper_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Whisper should request GPUs from Ray even when local CUDA is hidden.

    The ``WhisperPool`` actor runs with ``num_gpus=0``, so Ray hides CUDA in
    its process; the GPU request must come from Ray cluster resources, not
    from ``torch.cuda.is_available()``.
    """
    monkeypatch.setattr(whisper_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(whisper_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert whisper_workers._whisper_num_gpus(_config()) == 0.25


def test_whisper_num_gpus_is_zero_when_disabled_by_config(monkeypatch):
    """A non-positive configured request keeps Whisper off the GPU."""
    monkeypatch.setattr(whisper_workers.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(whisper_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert whisper_workers._whisper_num_gpus(_config(whisper_num_gpus=0)) == 0


def test_whisper_num_gpus_falls_back_to_cuda_when_ray_lookup_fails(monkeypatch):
    """If Ray cannot report resources, fall back to the local CUDA check."""

    def _boom():
        raise RuntimeError("ray not ready")

    monkeypatch.setattr(whisper_workers.ray, "cluster_resources", _boom)
    monkeypatch.setattr(whisper_workers.torch.cuda, "is_available", lambda: True)
    assert whisper_workers._whisper_num_gpus(_config()) == 0.25

    monkeypatch.setattr(whisper_workers.torch.cuda, "is_available", lambda: False)
    assert whisper_workers._whisper_num_gpus(_config()) == 0
