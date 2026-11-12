"""The port that builds the retrieval pipelines a set of partitions is searched with.

``RetrievalService`` fans a query out over partition groups and fuses their
results; which pipeline, searcher and reranker serve which partitions is
decided behind this port, by ``di.retrieval.RetrievalPipelineFactory``, which
resolves them from the partition configs and the model-endpoint catalog.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.rerankers.reranker import Reranker
    from core.retrieval.pipeline import RetrieverPipeline
    from core.retrieval.searcher import RetrievalSearcher


@dataclass(frozen=True, slots=True)
class PipelineGroup:
    """Partitions retrieved together by one pipeline.

    *default_top_k* is the number of chunks the pipeline keeps when the caller
    sets none. *reranker_identity* is the public description of the reranker
    it runs, ``None`` when it doesn't rerank.
    """

    partitions: tuple[str, ...]
    pipeline: RetrieverPipeline
    default_top_k: int | None
    reranker_identity: dict[str, object] | None


class PipelineFactory(Protocol):
    def pipeline_groups(
        self,
        partitions: Sequence[str],
        *,
        top_k: int | None,
        similarity_threshold: float | None,
        disable_reranker: bool,
        disable_expansion: bool,
    ) -> list[PipelineGroup]:
        """The configured *partitions* grouped by the pipeline that retrieves them, after the caller's overrides.

        Raises ``PartitionNotFoundError`` for a partition with no config.
        """
        ...

    def search_groups(self, partitions: list[str]) -> list[tuple[list[str], RetrievalSearcher]]:
        """The partitions grouped by the vector field they are searched on, each with its searcher.

        With a single group the caller's partitions are kept, so ``"all"``
        stays unscoped.
        """
        ...

    def reranker(self, name: str | None, partition: str) -> tuple[Reranker | None, dict[str, object]]:
        """The reranker endpoint *name* resolves to, else the default one, with its public identity.

        *partition* only names the caller in the logs.
        """
        ...
