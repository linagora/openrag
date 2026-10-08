from __future__ import annotations

import importlib.util
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "eval_query_decomposition",
    Path(__file__).parents[3] / "tests/load/prompt_eval/eval_query_decomposition.py",
)
assert _SPEC is not None and _SPEC.loader is not None
evaluator = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = evaluator
_SPEC.loader.exec_module(evaluator)


def test_production_contextualizer_template_renders_for_prompt_evaluation():
    template_path = Path(__file__).parents[3] / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    rendered = evaluator.format_prompt(template_path.read_text(), "Compare method A and method B")

    assert "{calendar_anchors}" not in rendered
    assert "Calendar anchors (use verbatim, do not recompute)" in rendered
    assert "Independent comparisons" in rendered
    assert "Return no more than 8 sub-queries total" in rendered
    assert rendered.index("Runtime calendar context") > rendered.index("Examples:")


def test_contextualizer_splits_comparisons_and_coarsens_long_trends():
    template_path = Path(__file__).parents[3] / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    rendered = evaluator.format_prompt(template_path.read_text(), "Compare method A and method B")

    assert "Independent comparisons" in rendered
    assert "ALWAYS emit one sub-query per item" in rendered
    assert "Carry every shared criterion into each item-focused query" in rendered
    assert "Do not combine both sides into one query" in rendered
    assert "split the comparison into one query per criterion" in rendered
    assert "group adjacent months into quarters" in rendered
    assert "Cover every part of the range without gaps or dropped later periods" in rendered
    assert "How did monthly rail delays change from January through December 2024?" in rendered
    assert "Rail delays from October through December 2024" in rendered


def test_issue_751_practical_comparison_case_expects_independent_queries():
    dataset_path = Path(__file__).parents[3] / "tests/load/prompt_eval/datasets/issue_751.json"
    cases = json.loads(dataset_path.read_text())
    comparison = next(case for case in cases if case["id"] == 3)

    assert len(comparison["expected_queries"]["query_list"]) == 2


def test_issue_751_cases_are_held_out_from_prompt_examples():
    root = Path(__file__).parents[3]
    dataset_path = root / "tests/load/prompt_eval/datasets/issue_751.json"
    template_path = root / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    cases = json.loads(dataset_path.read_text())
    examples = re.findall(r'^User: "(.+)"$', template_path.read_text(), re.MULTILINE)

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    for case in cases:
        question = normalize(case["messages"][-1]["content"])
        assert all(SequenceMatcher(None, question, normalize(example)).ratio() < 0.8 for example in examples)


def test_eval_messages_include_the_production_query_hint():
    messages = evaluator.build_llm_messages("contextualizer", [{"role": "user", "content": "compare A and B"}])

    assert messages[0]["content"].startswith("contextualizer")
    assert "query_list may contain one or more distinct sub-queries, up to 8" in messages[0]["content"]
    assert "user: compare A and B" in messages[1]["content"]


def test_issue_751_eval_cases_validate_against_the_production_query_schema():
    dataset_path = Path(__file__).parents[3] / "tests/load/prompt_eval/datasets/issue_751.json"
    cases = json.loads(dataset_path.read_text())

    for case in cases:
        evaluator.SearchQueries.model_validate(case["expected_queries"])
