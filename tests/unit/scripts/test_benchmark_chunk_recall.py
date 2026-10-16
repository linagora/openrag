"""Coverage metrics must count chunk identity, not source or duplicate hits."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "benchmark_chunk_recall", Path(__file__).parents[3] / "tests/load/prompt_eval/benchmark_chunk_recall.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_full_recall_requires_every_gold_chunk():
    assert benchmark.chunk_recall(["a", "b"], ["a", "a", "irrelevant"]) == {"recall": 0.5, "full_recall": False}
    assert benchmark.chunk_recall(["a", "b"], ["b", "a"]) == {"recall": 1.0, "full_recall": True}


def test_three_chunk_bucket_and_empty_gold():
    assert benchmark.chunk_recall(["a", "b", "c"], ["a", "b"])["full_recall"] is False
    with pytest.raises(ValueError):
        benchmark.chunk_recall([], ["a"])


def test_chunk_comparison_questions_do_not_duplicate_prompt_examples():
    import json
    import re
    from difflib import SequenceMatcher

    root = Path(__file__).parents[3]
    prompt = (root / "openrag/prompts/templates/query_contextualizer_tmpl.txt").read_text()
    examples = re.findall(r'^User: "(.+)"$', prompt, re.MULTILINE)
    cases = json.loads((root / "tests/load/prompt_eval/datasets/scifact_chunk_comparisons.json").read_text())
    assert len(cases) == len({case["id"] for case in cases}) == 12
    for case in cases:
        assert len(set(case["gold_chunk_ids"])) in (2, 3)
        for example in examples:
            assert SequenceMatcher(None, case["question"].casefold(), example.casefold()).ratio() < 0.8
