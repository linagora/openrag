"""What a request's partitions set for its prompt: how many sources it takes, and which answer template.

Read by the ``classic`` graph's steps and by the completions endpoint, so both
fit and render their sources the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.chat.budget import fit_sources
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens

if TYPE_CHECKING:
    from core.config.root import Settings

logger = get_logger()


class PartitionPromptSettings:
    """The partition-dependent settings of a request's prompt, read from the live config."""

    def __init__(self, config: Settings) -> None:
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config

    def top_n(self, partition: list[str] | None) -> int:
        """How many sources this request's prompt takes: the retrieval preset's ``top_n``.

        ``reranker.top_k`` when the preset leaves it unset. Retrieval already
        cuts each group of partitions it retrieves together (one embedder, the
        same retrieval settings) to the group's ``top_n``; this matters for a
        request whose partitions, or ``"all"``, span several groups, whose fused
        results can hold up to the *sum* of them. It takes the largest ``top_n``
        among the partitions, so about one group's worth (in RRF order) reaches
        the prompt.
        """
        default = self._config.reranker.top_k
        configs = self._config.partitions
        if partition and configs:
            names = list(configs) if "all" in partition else partition
            top_ns = [
                cfg.retrieval.effective_top_n(default) for name in names if (cfg := configs.get(name)) is not None
            ]
            if top_ns:
                return max(top_ns)
        return default

    def fit_sources(self, docs: list, partition: list[str] | None, headroom: int | None) -> list[int]:
        """Positions of up to ``top_n`` of *docs*, within the *headroom* the answering model's window leaves.

        See ``core.chat.budget.fit_sources``. The window comes from the LLM
        endpoint's context size (admin-set, else probed from vLLM, else
        ``MAX_LLM_CONTEXT_SIZE``), so a too-small value there shows up here as
        fewer sources than ``top_n``: logged, because nothing else surfaces it.
        """
        top_n = self.top_n(partition)
        included = fit_sources(docs, top_n=top_n, headroom=headroom, length_function=get_num_tokens())
        if len(included) < min(len(docs), top_n):
            logger.bind(partition=partition, top_n=top_n, presented=len(included), headroom=headroom).info(
                "Sources left out: they don't fit in the answering model's context window"
            )
        return included

    def generation_prompt_name(self, prompt_type: str, partition: list[str] | None) -> str | None:
        """The library prompt this request's partition names for a generation type.

        Honoured only for a single owning partition (same rule as chat_llm /
        chat_history_depth); multi-partition and the ``"all"`` sentinel resolve
        the global default. Returned as the sole candidate name for
        ``PromptService.resolve_prompt`` — a future per-user tier prepends ahead
        of it.
        """
        if not partition or "all" in partition or len(partition) != 1:
            return None
        cfg = self._config.partitions.get(partition[0])
        if cfg is None:
            return None
        return getattr(cfg, "generation_prompt_names", {}).get(prompt_type)


__all__ = ["PartitionPromptSettings"]
