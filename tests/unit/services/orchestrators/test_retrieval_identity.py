"""The public description of the retrieval settings, on its own."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.retrieval.trace import canonical_fingerprint
from services.orchestrators.retrieval_identity import RetrievalIdentity, public_prompt_identity


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        retriever=SimpleNamespace(top_k=6, related_limit=10, max_ancestor_depth=None),
        reranker=SimpleNamespace(enabled=True, top_k=5, model_name="static-reranker"),
        partitions={
            "p": PartitionConfig(
                name="p",
                embedder="embed-a",
                indexation=IndexationPipelineConfig(),
                retrieval=RetrievalPipelineConfig(enable_reranker=True, reranker="partition-reranker"),
            )
        },
        models=SimpleNamespace(
            embedder={"embed-a": SimpleNamespace(model_name="embedding-model", vector_field="vector_a")},
            reranker={"partition-reranker": SimpleNamespace(model_name="catalog-reranker")},
            llm={},
        ),
        llm=SimpleNamespace(model="static-llm"),
        vectordb=SimpleNamespace(hybrid_search=True),
        rag=SimpleNamespace(mode="ChatBotRag", max_contextualized_query_len=512),
    )


def _identity(*, catalog: bool = True, static_reranker: bool = True) -> RetrievalIdentity:
    return RetrievalIdentity(
        config=_config(),
        catalog_llms=catalog,
        catalog_rerankers=catalog,
        catalog_embedders=catalog,
        static_reranker=static_reranker,
    )


def test_a_prompt_is_described_from_a_mapping_or_an_object():
    expected = {"name": "n", "source": "named", "content_hash": "h"}
    assert public_prompt_identity(expected) == expected
    assert public_prompt_identity(SimpleNamespace(**expected)) == expected


@pytest.mark.parametrize(
    ("catalog", "static_reranker", "reranker"),
    [
        (True, True, {"name": "partition-reranker", "model": "catalog-reranker"}),
        (False, True, {"name": "default", "model": "static-reranker"}),
        (False, False, {"name": None, "model": None}),
    ],
)
def test_the_reranker_is_the_catalog_endpoint_else_the_static_one(catalog, static_reranker, reranker):
    (partition,) = _identity(catalog=catalog, static_reranker=static_reranker).public_configuration(["p"])["partitions"]
    assert {key: partition["reranker"][key] for key in ("name", "model")} == reranker


def test_the_fingerprint_is_the_public_configurations():
    identity = _identity()
    assert identity.fingerprint(["p"]) == canonical_fingerprint(identity.public_configuration(["p"]))


@pytest.mark.parametrize("rerank", [None, {"reranker": {"name": "r", "model": "m"}, "candidates": 20}])
def test_a_search_is_described_with_its_rerank_only_when_it_reranked(rerank):
    configuration = _identity().public_search_configuration(["p"], {"top_k": 5}, rerank=rerank)
    assert configuration["partitions"] == [
        {"name": "p", "embedder": {"name": "embed-a", "model": "embedding-model", "vector_field": "vector_a"}}
    ]
    assert configuration.get("rerank") == rerank


@pytest.mark.asyncio
async def test_the_resolved_configuration_carries_the_request_overrides_and_its_prompt():
    prompt = {"name": "ctx", "source": "named", "content_hash": "abc"}
    public, fingerprint = await _identity().resolved_configuration(
        ["p"],
        reranker_identities={"p": {"name": "resolved", "model": "resolved-model"}},
        top_k=3,
        similarity_threshold=0.9,
        disable_reranker=True,
        disable_expansion=True,
        # Given, so it isn't resolved: there is no prompt service nor disk seed here.
        contextualizer_prompt=prompt,
        strict_prompt_resolution=False,
    )
    (partition,) = public["partitions"]
    assert partition["retrieval"]["top_k"] == 3
    assert partition["retrieval"]["similarity_threshold"] == 0.9
    assert partition["reranker"]["name"] == "resolved"
    assert partition["reranker"]["enabled"] is False
    assert partition["expansion"]["include_related"] is False
    assert public["contextualizer_prompt"] == prompt
    assert fingerprint == canonical_fingerprint(public)
