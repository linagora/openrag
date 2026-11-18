"""What a request's partitions set for its prompt: how much history, how many sources, which answer template.

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

    #: Used when the global ``rag.chat_history_depth`` config is < 1 — see
    #: PartitionService._CHAT_HISTORY_DEPTH_DEFAULT for the matching partition-side clamp.
    _CHAT_HISTORY_DEPTH_DEFAULT = 4

    def __init__(self, config: Settings) -> None:
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config
        # RAGConfig.chat_history_depth carries no lower bound, so a deployment may
        # configure it to 0 (or negative). Left unclamped, messages[-0:] would keep
        # the *entire* history instead of none — the opposite of what this depth
        # is meant to limit. Clamp here rather than reject at config-load time so a
        # misconfigured global default degrades safely instead of crashing startup.
        self._default_chat_history_depth = (
            config.rag.chat_history_depth if config.rag.chat_history_depth >= 1 else self._CHAT_HISTORY_DEPTH_DEFAULT
        )

    def chat_history_depth(self, partition: list[str] | None) -> int:
        """Effective chat-history depth for this request.

        Honors a partition's configured ``chat_history_depth`` (set via the
        admin API) over the global default. The admin API now requires an
        explicit ``chat_history_depth >= 1`` on create/update (``ge=1`` in
        ``CreatePartitionRequest``/``UpdatePartitionRequest``), and
        ``PartitionService.resolve_partition_row`` normalizes any pre-existing
        row still holding the legacy ``0`` sentinel to the global default
        value before it reaches ``Settings.partitions``. So in practice
        ``cfg.chat_history_depth`` is always >= 1 here; the ``> 0`` filter
        below is kept only as a defensive backstop — it must never reach the
        ``messages[-depth:]`` slice, where ``0`` would select the *entire*
        history rather than none.

        The ``"all"`` sentinel (``openrag-all``) reaches this layer un-expanded
        (retrieval resolves it to concrete partitions downstream) and is a
        cross-partition query with no single owning partition, so it uses the
        global default. A request scoped to one or more named partitions takes
        the largest explicit (>0) value among them.
        """
        if partition and "all" not in partition:
            explicit = [
                cfg.chat_history_depth
                for name in partition
                if (cfg := self._config.partitions.get(name)) is not None and cfg.chat_history_depth > 0
            ]
            if explicit:
                return max(explicit)
        return self._default_chat_history_depth

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
