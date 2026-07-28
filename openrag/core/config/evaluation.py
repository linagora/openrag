"""Configuration for the admin evaluation feature.

Operational limits live here rather than as constants in the code that uses
them, so a deployment can be retuned without a rebuild.

Domain contracts stay out of this file: the reserved partition prefix, the CSV
column names and the ``file_id`` alphabet are not settings, and changing them
would invalidate stored datasets.
"""

from __future__ import annotations

from pydantic import Field

from .base import ConfigMixin


class EvaluationConfig(ConfigMixin):
    """Limits and timeouts for evaluation datasets and runs.

    Every field is bounded: these are all reachable from the environment, and a
    non-positive limit does not degrade gracefully — it reaches the runner as a
    cap that rejects every upload, or as a timeout that expires instantly. A
    typo should fail at config load, where the message names the field.
    """

    #: Executable the runner shells out to; the images install it on PATH.
    promptfoo_bin: str = Field(default="promptfoo", min_length=1)

    #: Upload caps. A dataset is re-indexed on every run, so an oversized
    #: corpus costs far more than the upload itself.
    max_corpus_mb: int = Field(default=512, gt=0)
    max_testset_mb: int = Field(default=5, gt=0)
    #: Each test-set row costs one retrieval call plus one graded generation.
    max_testset_rows: int = Field(default=500, gt=0)

    #: Chunks retrieved per question by the retrieval config. Bounded like the
    #: retrieval pipeline's own ``top_k``.
    top_k: int = Field(default=5, gt=0, le=1000)

    #: How long to wait for one file's indexing task, and how often to poll it.
    task_timeout_seconds: float = Field(default=1800.0, gt=0)
    task_poll_seconds: float = Field(default=1.0, gt=0)

    #: Per-request timeout for the runner's own HTTP calls.
    http_timeout_seconds: float = Field(default=300.0, gt=0)

    #: promptfoo grades every row with an LLM, so allow for a slow grader.
    promptfoo_timeout_seconds: float = Field(default=3600.0, gt=0)

    @property
    def max_corpus_bytes(self) -> int:
        return self.max_corpus_mb * 1024 * 1024

    @property
    def max_testset_bytes(self) -> int:
        return self.max_testset_mb * 1024 * 1024


__all__ = ["EvaluationConfig"]
