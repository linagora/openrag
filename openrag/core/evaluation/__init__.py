"""Pure evaluation logic: test-set parsing, promptfoo config, metric math."""

from core.evaluation.metrics import extract_results, indexing_metrics, summarize
from core.evaluation.promptfoo_config import build_answer_config, build_retrieval_config
from core.evaluation.testset import parse_testset

__all__ = [
    "build_answer_config",
    "build_retrieval_config",
    "extract_results",
    "indexing_metrics",
    "parse_testset",
    "summarize",
]
