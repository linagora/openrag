"""Configuration for the admin evaluation feature.

Every operational limit a run depends on lives here rather than as a constant
in the code that uses it, so an operator can retune a deployment — a slow
grader, a large corpus, a long-running indexer — without a rebuild.

Domain constants stay out of this file on purpose: the reserved partition
prefix, the CSV column names and the ``file_id`` alphabet are contracts, not
settings, and changing them would silently invalidate stored datasets.
"""

from __future__ import annotations

from .base import ConfigMixin


class EvaluationConfig(ConfigMixin):
    """Limits and timeouts for evaluation datasets and runs."""

    #: Base URL the runner uses to reach the API. It drives OpenRAG through
    #: HTTP rather than in-process calls, and runs in its own container, so it
    #: addresses the API by service name rather than the admin's browser host.
    internal_url: str = "http://openrag:8080"

    #: Executable the runner shells out to. Both images install a pinned
    #: promptfoo on PATH.
    promptfoo_bin: str = "promptfoo"

    #: Upload caps. A dataset is re-indexed on every run, so an oversized
    #: corpus costs far more than the upload itself.
    max_corpus_mb: int = 512
    max_testset_mb: int = 5
    #: Each test-set row costs one retrieval call plus one graded generation.
    max_testset_rows: int = 500

    #: Chunks retrieved per question by the retrieval config.
    top_k: int = 5

    #: How long to wait for one file's indexing task, and how often to poll it.
    task_timeout_seconds: float = 1800.0
    task_poll_seconds: float = 1.0

    #: Per-request timeout for the runner's own HTTP calls.
    http_timeout_seconds: float = 300.0

    #: promptfoo grades every row with an LLM, so allow for a slow grader.
    promptfoo_timeout_seconds: float = 3600.0

    @property
    def max_corpus_bytes(self) -> int:
        return self.max_corpus_mb * 1024 * 1024

    @property
    def max_testset_bytes(self) -> int:
        return self.max_testset_mb * 1024 * 1024


__all__ = ["EvaluationConfig"]
