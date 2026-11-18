"""Unit tests for :class:`di.retrieval.RetrievalPipelineFactory`.

Which pipeline, searcher and reranker serve which partitions. The fan-out over
the groups and their fusion are ``RetrievalService``'s, tested in
``tests/unit/services/orchestrators/test_retrieval_service.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.utils.exceptions import PartitionNotFoundError
from di.retrieval import RetrievalPipelineFactory


class _Named:
    """A searcher or reranker that only says which endpoint it was built for."""

    def __init__(self, name: str):
        self.name = name


def _config(partitions: dict[str, PartitionConfig]) -> SimpleNamespace:
    return SimpleNamespace(
        retriever=SimpleNamespace(top_k=6),
        reranker=SimpleNamespace(enabled=True, top_k=5, model_name="static-model"),
        partitions=partitions,
        models=SimpleNamespace(
            embedder={
                "embed-a": SimpleNamespace(vector_field="vector_a", model_name="model-a"),
                "embed-b": SimpleNamespace(vector_field="vector_b", model_name="model-b"),
            },
            reranker={"rerank-x": SimpleNamespace(model_name="model-x")},
        ),
    )


def _partition(name: str, embedder: str = "embed-a", **retrieval) -> PartitionConfig:
    return PartitionConfig(
        name=name,
        embedder=embedder,
        indexation=IndexationPipelineConfig(),
        retrieval=RetrievalPipelineConfig(**retrieval),
    )


def _catalog_reranker(name: str):
    if name not in {"rerank-x", "default"}:
        raise KeyError(name)
    return _Named(name)


def _factory(*partitions: PartitionConfig, catalog: bool = True, static_reranker: bool = True):
    return RetrievalPipelineFactory(
        config=_config({p.name: p for p in partitions}),
        searcher=_Named("static"),
        reranker=_Named("static") if static_reranker else None,
        searcher_factory=_Named if catalog else None,
        reranker_factory=_catalog_reranker if catalog else None,
    )


def test_partitions_share_a_pipeline_only_with_the_same_vector_field_and_settings():
    factory = _factory(
        _partition("a1"),
        _partition("a2"),
        _partition("a3", rrf_k=42),
        _partition("b1", embedder="embed-b"),
    )

    groups = factory.pipeline_groups(["a1", "a2", "a3", "b1"])

    assert [group.partitions for group in groups] == [("a1", "a2"), ("a3",), ("b1",)]
    assert [group.pipeline.retriever.searcher.name for group in groups] == ["embed-a", "embed-a", "embed-b"]
    assert [group.pipeline.rrf_k for group in groups] == [60, 42, 60]


def test_each_group_carries_the_reranker_it_runs():
    factory = _factory(
        _partition("named", reranker="rerank-x"), _partition("plain"), _partition("off", enable_reranker=False)
    )

    named, plain, off = factory.pipeline_groups(["named", "plain", "off"])

    assert named.pipeline.reranker.name == "rerank-x"
    assert named.reranker_identity == {"name": "rerank-x", "model": "model-x"}
    assert plain.pipeline.reranker.name == "default"
    assert plain.reranker_identity == {"name": "default", "model": None}
    assert off.pipeline.reranker is None
    assert off.reranker_identity is None


def test_the_callers_overrides_decide_the_grouping():
    factory = _factory(_partition("p1", reranker="rerank-x"), _partition("p2", top_k=20))

    assert len(factory.pipeline_groups(["p1", "p2"])) == 2
    (group,) = factory.pipeline_groups(["p1", "p2"], top_k=10, disable_reranker=True)
    assert group.partitions == ("p1", "p2")
    assert group.pipeline.retriever.top_k == 10
    assert group.default_top_k == 5


def test_a_partition_without_a_config_is_not_found():
    with pytest.raises(PartitionNotFoundError):
        _factory(_partition("known")).pipeline_groups(["known", "unknown"])


@pytest.mark.parametrize(
    ("name", "catalog", "static_reranker", "expected"),
    [
        ("rerank-x", True, True, ("rerank-x", {"name": "rerank-x", "model": "model-x"})),
        # A stale name falls back to the catalog default.
        ("renamed", True, True, ("default", {"name": "default", "model": None})),
        (None, True, True, ("default", {"name": "default", "model": None})),
        # Without the catalog, the reranker built at startup from settings.reranker.
        ("rerank-x", False, True, ("static", {"name": "default", "model": "static-model"})),
        ("rerank-x", False, False, (None, {"name": None, "model": None})),
    ],
)
def test_reranker_resolution_order(name, catalog, static_reranker, expected):
    factory = _factory(catalog=catalog, static_reranker=static_reranker)

    reranker, identity = factory.reranker(name, "p")

    assert (reranker.name if reranker is not None else None, identity) == expected


def test_search_groups_split_by_vector_field_and_keep_a_single_groups_scope():
    factory = _factory(_partition("a1"), _partition("a2"), _partition("b1", embedder="embed-b"))

    split = factory.search_groups(["a1", "b1"])
    (single,) = factory.search_groups(["a1", "a2"])

    assert [(names, searcher.name) for names, searcher in split] == [(["a1"], "embed-a"), (["b1"], "embed-b")]
    assert (single[0], single[1].name) == (["a1", "a2"], "embed-a")


def test_all_stays_unscoped_when_every_partition_is_searched_together():
    factory = _factory(_partition("a1"), _partition("a2"))

    ((names, searcher),) = factory.search_groups(["all"])

    assert (names, searcher.name) == (["all"], "embed-a")


def test_without_a_searcher_factory_every_partition_uses_the_default_searcher():
    factory = _factory(_partition("a1"), _partition("b1", embedder="embed-b"), catalog=False)

    ((names, searcher),) = factory.search_groups(["a1", "b1"])
    (group,) = factory.pipeline_groups(["a1"])

    assert (names, searcher.name) == (["a1", "b1"], "static")
    assert group.pipeline.retriever.searcher.name == "static"
