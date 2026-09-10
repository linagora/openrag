"""Tests for the pipeline/actor provenance seam (#762 E)."""

from __future__ import annotations

import pytest


class _Embedder:
    def __init__(self, dimension=1024, model="Qwen3-Embedding-0.6B", endpoint="https://x/v1"):
        self._dimension = dimension
        self._model = model
        self._endpoint = endpoint

    async def embed(self, texts):
        return [[0.0] * (self._dimension or 1)] * len(texts)

    async def embed_single(self, text):
        return [0.0] * (self._dimension or 1)

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise RuntimeError("Embedding dimension unknown — call embed() first")
        return self._dimension

    @property
    def model_name(self):
        return self._model

    @property
    def endpoint(self):
        return self._endpoint


def test_provenance_records_what_the_reference_resolved_to():
    """The endpoint name alone cannot catch a repointed endpoint — the
    model_name/endpoint pair is the only thing that does (problem C)."""
    from services.workers.pipeline_builder import _embedder_provenance

    prov = _embedder_provenance(_Embedder(), "Qwen3-Embedding-0.6B")

    assert prov == {
        "embedder": "Qwen3-Embedding-0.6B",
        "embedder_model_name": "Qwen3-Embedding-0.6B",
        "embedder_endpoint": "https://x/v1",
        "embedder_dimension": 1024,
    }


def test_provenance_keeps_the_default_alias_as_written():
    """The record shows what was *asked for*; the resolved model name beside it
    shows what that meant at the time."""
    from services.workers.pipeline_builder import _embedder_provenance

    prov = _embedder_provenance(_Embedder(), "default")

    assert prov["embedder"] == "default"
    assert prov["embedder_model_name"] == "Qwen3-Embedding-0.6B"


def test_provenance_survives_an_embedder_that_never_ran():
    """A file that produced no chunks has no dimension to record. Describing
    the run must not fail the run."""
    from services.workers.pipeline_builder import _embedder_provenance

    prov = _embedder_provenance(_Embedder(dimension=None), None)

    assert prov["embedder"] == "default"
    assert prov["embedder_dimension"] is None


def test_merge_copies_rather_than_mutating_the_dispatched_config():
    """`indexation_config` is still read after the catalog write (topic tags,
    the active-config contextvar) and must stay the config that was dispatched."""
    from services.workers.indexer_actor import _with_embedder_provenance

    dispatched = {"parsing_strategy": "marker"}
    merged = _with_embedder_provenance(dispatched, {"embedder": "bge-m3"})

    assert merged == {"parsing_strategy": "marker", "embedder": "bge-m3"}
    assert dispatched == {"parsing_strategy": "marker"}


def test_merge_records_provenance_even_without_a_preset_snapshot():
    """Legacy passthrough dispatches no config; which embedder ran is still
    worth a row."""
    from services.workers.indexer_actor import _with_embedder_provenance

    assert _with_embedder_provenance(None, {"embedder": "bge-m3"}) == {"embedder": "bge-m3"}


@pytest.mark.parametrize("provenance", [None, {}])
def test_merge_leaves_the_config_alone_when_there_is_nothing_to_record(provenance):
    from services.workers.indexer_actor import _with_embedder_provenance

    dispatched = {"parsing_strategy": "marker"}
    assert _with_embedder_provenance(dispatched, provenance) is dispatched
