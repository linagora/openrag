"""Pure evaluation logic: test-set parsing, promptfoo config, metric math."""

from core.evaluation.identity import sanitize_file_id
from core.evaluation.testset import parse_testset

__all__ = [
    "parse_testset",
    "sanitize_file_id",
]
