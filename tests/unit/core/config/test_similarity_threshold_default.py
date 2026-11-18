"""The similarity threshold defaults to 0.2 everywhere it has a default."""

from __future__ import annotations

import inspect

from api.routers.user.search import CommonSearchParams
from core.config.mcp import MCPServerConfig
from core.config.retrieval import RetrieverConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.query import RetrievalQuery
from core.retrieval.retriever import Retriever
from services.orchestrators.mcp_service import MCPService
from services.orchestrators.preset_service import _DEFAULT_SEEDS


def test_config_and_model_defaults() -> None:
    assert RetrieverConfig().similarity_threshold == 0.2
    assert RetrievalPipelineConfig().similarity_threshold == 0.2
    assert MCPServerConfig().similarity_threshold == 0.2
    assert RetrievalQuery.model_fields["similarity_threshold"].default == 0.2


def test_default_retrieval_seed() -> None:
    assert _DEFAULT_SEEDS["retrieval"]["default"]["similarity_threshold"] == 0.2


def test_search_endpoint_default() -> None:
    default = inspect.signature(CommonSearchParams.__init__).parameters["similarity_threshold"].default
    assert default.default == 0.2


def test_retriever_and_mcp_service_defaults() -> None:
    for fn in (Retriever.__init__, MCPService.__init__):
        assert inspect.signature(fn).parameters["similarity_threshold"].default == 0.2
