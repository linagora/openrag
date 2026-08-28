"""Central configuration for the automatic evaluation pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CommonConfig:
    """Shared across benchmark, ablation, generation, and upload."""
    partition: str = field(default_factory=lambda: os.environ.get("PARTITION", "test3"))
    base_url: str = field(
        default_factory=lambda: os.environ.get("API_BASE_URL", "http://localhost:8080")
    )
    dataset_path: str = "./dataset.json"
    output_dir: str = "./reports"


@dataclass
class TargetModelConfig:
    """Parameters for calling the OpenRAG endpoint under evaluation."""

    temperature: float = 0.2
    logprobs: bool = True
    # 300s (was 120): the shared remote LLM occasionally stalls a single
    # query past 2min under concurrency; give it more time before dropping.
    timeout: int = 300


@dataclass
class JudgeConfig:
    """LLM-as-judge sampling params (model / base_url / api_key come from env)."""

    temperature: float = 0.1
    max_tokens: int = 4000
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    retry_attempts: int = 3  # JSON-parse retries per judge call


@dataclass
class BenchmarkConfig:
    target: TargetModelConfig = field(default_factory=TargetModelConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)

    # Concurrency limits (asyncio semaphores)
    openrag_concurrency: int = 10
    judge_concurrency: int = 10
    chunk_fetch_concurrency: int = 16
    chunk_fetch_timeout: int = 30

    # Response labelling
    label_language: Literal["en", "fr"] = "en"

    # Chain-of-thought audit (random subsample re-scored with CoT prompts)
    cot_audit_fraction: float = 0.10
    cot_audit_seed: int = 42

    # Faithfulness judging (slowest judge — scored on a random sample only)
    faithfulness_fraction: float = 0.5
    faithfulness_seed: int = 42

    # Document-category (content domain) breakdown — "research_paper", "hr_policy",
    # "support_documentation", "financial_filing", … The taxonomy is DISCOVERED from
    # the corpus at runtime rather than hardcoded, so new domains appear on their own
    # instead of being forced into a fixed enum. Unambiguous filename patterns
    # (arXiv id, SEC filing code, chapter_N) are matched for free; the rest are
    # classified in batches by the judge LLM (~8s one-time on a 156-doc corpus).
    # Taxonomy + assignments are cached to `document_categories.json` next to the
    # dataset: repeat runs cost nothing, and the file can be hand-edited to correct a
    # label (or deleted to re-discover).
    classify_document_categories: bool = True
    # Optional steer for the discovery pass — examples only, NOT a closed list.
    # Leave empty for a fully data-driven taxonomy.
    document_category_hints: tuple[str, ...] = ()
    document_category_max_labels: int = 12
    document_category_sample_docs: int = 40
    document_category_sample_chars: int = 1200
    document_category_batch_size: int = 10

    # Exact-value (numeric-grounding) scoring for capability-suite value-answer
    # question types. Complements the LLM judge with a strict check that every
    # number in the golden answer appears in the response (within tolerance). Inert
    # for datasets whose rows aren't these types (e.g. the distribution profile).
    value_answer_types: tuple[str, ...] = ("table_lookup", "numerical_reasoning")
    numeric_rel_tol: float = 0.01
    numeric_abs_tol: float = 1e-9

    # Debug: cap dataset to N entries (None = all)
    limit: int | None = None
    # Questions sampled for the ablation when run via `benchmark.py --ablation`
    ablation_limit: int = 5

    # Raw retriever ranking trace: for every answerable row with ground-truth
    # chunks, hits OpenRAG's /search endpoint directly (bypassing the LLM and
    # its citation filtering) to record the retriever's actual ranked output.
    # Written to retrieval_trace_<ts>.jsonl / .csv. Gated by the same
    # --no-retrieval flag as the ID-based retrieval metrics.
    retrieval_trace_top_k: int = 10
    retrieval_trace_snippet_chars: int = 240

    # Which OpenRAG retriever.type (single/multiQuery/hyde) the target instance is
    # believed to be running. LABEL ONLY — the benchmark has no way to change or
    # verify the deployed instance's actual config; setting this does not touch
    # RETRIEVER_TYPE on the target. It exists so runs comparing strategies (e.g.
    # single vs multiQuery) are self-documenting instead of relying on the
    # operator to remember which .env was live for which run. None = unrecorded
    # (legacy runs, or runs where the strategy wasn't tracked).
    retriever_type: Literal["single", "multiQuery", "hyde"] | None = None


@dataclass
class AblationConfig:
    """Standalone `context_ablation.py` run (closed-book vs OpenRAG)."""

    temperature: float = 0.2
    timeout: int = 120
    limit: int = 5
    openrag_concurrency: int = 10
    closed_book_concurrency: int = 10
    csv_name: str = "context_ablation.csv"


@dataclass
class ClusteringConfig:
    method: Literal["hdbscan", "kmeans", "dbscan"] = "hdbscan"

    # UMAP dimensionality reduction (applied before clustering)
    umap_n_neighbors: int = 15
    umap_n_components: int = 5
    umap_min_dist: float = 0.1
    umap_metric: str = "cosine"
    # Seed UMAP for reproducible embeddings across runs. Note: a fixed random_state
    # forces UMAP to run single-threaded (it warns), trading speed for determinism.
    umap_random_state: int = 42

    # KMeans (used when method == "kmeans")
    kmeans_random_state: int = 42
    kmeans_n_init: int = 10

    # DBSCAN (used when method == "dbscan")
    dbscan_eps: float = 0.1
    dbscan_min_samples: int = 3
    dbscan_metric: str = "cosine"


@dataclass
class FiltrationConfig:
    """LLM-critic quality gate run on each generated item before it is kept.

    A critic call scores answerability / self-containment / clarity (and, for
    answerable items, faithfulness of the golden answer; for unanswerable items,
    that the answer is genuinely absent). Items below threshold are regenerated
    up to ``max_quality_retries`` times. See gaps (1)–(3) in the eval review.
    """

    enabled: bool = True
    # Answerable-item thresholds (critic scores are 0.0–1.0).
    answerability_threshold: float = 0.7
    self_containment_threshold: float = 0.7
    clarity_threshold: float = 0.6
    # Require the generated golden answer to be fully grounded in its chunks.
    require_faithful: bool = True
    # Regeneration budget per item when the critic rejects it.
    max_quality_retries: int = 2
    # If True, items that never pass the critic are excluded from the dataset.
    # Default False keeps them (flagged metadata.quality.passed=False) so the
    # dataset size stays predictable and the failures remain auditable.
    drop_failed: bool = True
    # Critic sampling temperature (low for deterministic scoring).
    critic_temperature: float = 0.0


@dataclass
class TypingConfig:
    """Typed-question diversity (gaps (4)–(5) in the eval review).

    Each answerable question is assigned a type sampled from ``distribution``;
    the type drives a type-specific generation instruction, the minimum number
    of chunks, and the difficulty/n_hops metadata. Keys MUST match
    ``QUESTION_TYPE_INSTRUCTIONS`` in ``evaluation_prompts.py``.
    """

    enabled: bool = True
    # Sampling weights per question type (normalized internally by random.choices).
    distribution: dict[str, float] = field(
        default_factory=lambda: {
            "single_hop_specific": 0.40,
            "multi_hop_specific": 0.25,
            "reasoning": 0.20,
            "comparative": 0.15,
        }
    )


@dataclass
class ChunkFilterConfig:
    """Drop low-information chunks before embedding/clustering/sampling.

    Bibliographies, acknowledgements and copyright boilerplate yield metadata trivia
    ("which authors wrote X?") that tests nothing about retrieval quality, and they
    skew the clusters. Deterministic and cheap — no LLM call. See gap (6) in the
    eval review.
    """

    enabled: bool = True
    # Minimum body length (after stripping the [CONTEXT] preamble and markers).
    min_chars: int = 200
    # Boilerplate when >= this fraction of the body sits under a References /
    # Acknowledgements / Conflict-of-Interest style heading.
    boilerplate_fraction: float = 0.5
    # Bibliography at this many citation-entry LINES ("[1] A. Cayley, ...").
    # Line-shaped on purpose: bare "[1]" also occurs inside maths formulae.
    min_citation_lines: int = 3


@dataclass
class CapabilitySuiteConfig:
    """Second generation profile: a fixed-count *capability suite* (vs the default
    cluster + type-distribution profile). Selected via ``question_gen.profile``.

    Instead of "N per cluster from a type distribution", it generates a target
    number of questions PER CAPABILITY, drawing from capability-appropriate chunks
    (tables for table_lookup, number-rich for numerical_reasoning, distinct files
    for cross_document_reasoning, many chunks for long_context_retrieval, …).
    """

    # Default target per capability (user-configurable, hence the name).
    per_category: int = 50
    # The 7 feasible capabilities. Remove one to skip it entirely.
    enabled_categories: tuple[str, ...] = (
        "table_lookup",
        "multi_hop_retrieval",
        "cross_document_reasoning",
        "citation_grounding",
        "unanswerable",
        "long_context_retrieval",
        "numerical_reasoning",
    )
    # Per-capability count overrides; anything not listed uses ``per_category``.
    # e.g. {"table_lookup": 20, "unanswerable": 0} — 0 disables that one.
    count_overrides: dict[str, int] = field(default_factory=dict)

    # Loop-until-count controls: over-generate each round to absorb critic/gate
    # rejections, and cap the number of rounds so a hard-to-fill capability
    # (e.g. no table chunks) can't loop forever.
    oversample: float = 1.4
    max_rounds: int = 8

    # long_context_retrieval sampling breadth (answer must span many chunks).
    long_context_min_chunks: int = 5
    long_context_max_chunks: int = 10
    # Min numeric tokens for a chunk to qualify for numerical_reasoning.
    numeric_min_tokens: int = 6


@dataclass
class QuestionGenConfig:
    # Dataset item schema version + generation-prompt version, stamped into
    # each item's metadata.generation block and the sidecar manifest.
    schema_version: str = "2.0"
    prompt_version: str = "qgen-v3"

    # Generation profile: "distribution" (default — cluster + type distribution)
    # or "capability_suite" (fixed count per capability). Kept side-by-side so a
    # user can switch between them; neither overrides the other.
    profile: Literal["distribution", "capability_suite"] = "distribution"

    # Generation-model sampling
    temperature: float = 0.2
    max_retries: int = 3
    timeout: int = 60
    max_tokens: int = 2048
    stop_after_attempt: int = 2

    # Max concurrent generator-LLM calls during dataset generation. One shared
    # semaphore is created inside the event loop and passed to every task.
    gen_concurrency: int = 10

    # Seed for chunk sampling (random.sample / random.randint) so a given partition
    # produces a reproducible dataset across runs.
    seed: int = 42

    # Chunk retrieval from the partition.
    # Large partitions return all chunks WITH embeddings in one response
    fetch_retries: int = 3
    fetch_timeout: int = 600

    # Question mix generated per cluster
    language: Literal["en", "fr"] = "en"
    n_min: int = 1
    n_max: int = 3
    n_questions_per_cluster: int = 2
    n_unanswerable_per_cluster: int = 1

    # Document content-domain classification, baked into each item's
    # metadata.source.document_category at build time so every benchmark run over the
    # dataset slices on identical labels. Taxonomy is DISCOVERED from the corpus at
    # runtime (never hardcoded); unambiguous filenames are matched free, the rest are
    # classified in batches and cached in .doc_categories/<partition>.json.
    classify_document_categories: bool = True
    # Optional steer for discovery — examples only, NOT a closed list.
    document_category_hints: tuple[str, ...] = ()
    document_category_max_labels: int = 12
    document_category_sample_docs: int = 40
    document_category_sample_chars: int = 1200
    document_category_batch_size: int = 10

    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    filtration: FiltrationConfig = field(default_factory=FiltrationConfig)
    typing: TypingConfig = field(default_factory=TypingConfig)
    chunk_filter: ChunkFilterConfig = field(default_factory=ChunkFilterConfig)
    capability_suite: CapabilitySuiteConfig = field(default_factory=CapabilitySuiteConfig)


@dataclass
class UploadConfig:
    dir_path: str = "./pdf_files"
    max_retries: int = 3
    retry_backoff: int = 2
    upload_timeout: int = 30
    health_check_timeout: int = 5

    # SCORE service
    score_client_timeout: int = 60
    score_job_timeout: int = 600
    score_poll_interval: int = 3


@dataclass
class Config:
    common: CommonConfig = field(default_factory=CommonConfig)
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)
    ablation: AblationConfig = field(default_factory=AblationConfig)
    question_gen: QuestionGenConfig = field(default_factory=QuestionGenConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)


CONFIG = Config()
