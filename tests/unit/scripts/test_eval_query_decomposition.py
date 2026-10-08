from __future__ import annotations

import importlib.util
import json
import sys
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
    assert "Conceptual or method comparisons" in rendered


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
