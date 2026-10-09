from __future__ import annotations

import importlib.util
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert "ALWAYS emit exactly one sub-query per item" in rendered
    assert "Carry every shared criterion into each item-focused query" in rendered
    assert "Do not combine both sides into one query" in rendered
    assert "split the comparison into one query per criterion" in rendered
    assert "group adjacent months into quarters" in rendered
    assert "Cover every part of the range without gaps or dropped later periods" in rendered
    assert "How did monthly rail delays change from January through December 2024?" in rendered
    assert "Rail delays from October through December 2024" in rendered


def test_comparison_examples_keep_each_query_focused_on_one_item():
    template_path = Path(__file__).parents[3] / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    rendered = evaluator.format_prompt(template_path.read_text(), "Compare method A and method B")
    comparison_examples = rendered.split("# Temporal filters", maxsplit=1)[0]
    comparison_outputs = "\n".join(line for line in comparison_examples.splitlines() if line.startswith('{"intent"'))

    assert "compared with" not in comparison_outputs.casefold()
    assert "PBiGaBP framework soft interference cancellation" in comparison_outputs
    assert "Tensor decomposition approach in hybrid RIS systems and how it handles interference" in comparison_outputs
    assert "SpecVQGAN generates audio and its audio generation quality" in comparison_outputs
    assert "DiffFoley generates audio and its audio generation quality" in comparison_outputs
    assert "Payload capacity and refueling time for electric delivery vans" in comparison_outputs
    assert "Payload capacity and refueling time for hydrogen fuel-cell vans" in comparison_outputs


def test_issue_751_practical_comparison_case_expects_independent_queries():
    dataset_path = Path(__file__).parents[3] / "tests/load/prompt_eval/datasets/issue_751.json"
    cases = json.loads(dataset_path.read_text())
    comparison = next(case for case in cases if case["id"] == 3)

    queries = [item["query"] for item in comparison["expected_queries"]["query_list"]]
    assert len(queries) == 2
    assert "geothermal heat pumps" in queries[0]
    assert "air-source heat pumps" in queries[1]
    assert all("installation cost" in query.casefold() and "winter efficiency" in query.casefold() for query in queries)


def test_comparison_policy_cases_keep_both_develop_and_current_labels():
    dataset_path = Path(__file__).parents[3] / "tests/load/prompt_eval/datasets/query_decomposition.json"
    cases = json.loads(dataset_path.read_text())
    by_id = {case["id"]: case for case in cases}

    for case_id in (64, 68, 73, 80):
        case = by_id[case_id]
        assert len(case["develop_expected_queries"]) == 1
        assert len(case["expected_queries"]["query_list"]) == 2
        assert len(case["comparison_policy"]["items"]) == 2
        assert case["comparison_policy"]["shared_criteria"]


def test_comparison_policy_requires_one_item_and_all_shared_criteria_per_query():
    policy = {"items": ["item A", "item B"], "shared_criteria": ["cost", "efficiency"]}

    assert evaluator.score_comparison_policy(
        policy,
        ["Cost and efficiency for item A", "Cost and efficiency for item B"],
    ) == (True, None)
    passed, reason = evaluator.score_comparison_policy(
        policy,
        ["Cost and efficiency for item A and item B", "Cost and efficiency for item A and item B"],
    )
    assert passed is False
    assert "exactly one compared item" in reason


def test_eval_cases_are_held_out_from_prompt_examples():
    root = Path(__file__).parents[3]
    template_path = root / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    datasets = [
        root / "tests/load/prompt_eval/datasets/issue_751.json",
        root / "tests/load/prompt_eval/datasets/query_decomposition.json",
    ]
    examples = re.findall(r'^User: "(.+)"$', template_path.read_text(), re.MULTILINE)

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    for dataset_path in datasets:
        cases = json.loads(dataset_path.read_text())
        for case in cases:
            question = normalize(case["messages"][-1]["content"])
            assert all(SequenceMatcher(None, question, normalize(example)).ratio() < 0.8 for example in examples), (
                f"{dataset_path.name} case {case['id']} overlaps a prompt example"
            )


@pytest.mark.asyncio
async def test_eval_reports_relabelled_cases_against_develop_and_current_labels():
    class Generator:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "query_list": [
                            {"query": "criterion for item A"},
                            {"query": "criterion for item B"},
                        ]
                    }
                )
            )

    class Judge:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, _messages):
            return evaluator.CoverageJudgment(covered=True)

    case = {
        "id": 64,
        "difficulty": 2,
        "domain": "law",
        "messages": [{"role": "user", "content": "Compare item A and item B by one criterion."}],
        "expected_queries": {"query_list": [{"query": "criterion for item A"}, {"query": "criterion for item B"}]},
        "develop_expected_queries": ["Comparison of item A and item B by one criterion"],
        "comparison_policy": {"items": ["item A", "item B"], "shared_criteria": ["criterion"]},
    }

    result = await evaluator.run_case(case, "test prompt", Generator(), "model", Judge(), "judge")
    report = evaluator.build_model_report([result], "model", "prompt", "dataset", "judge")

    assert result.develop_label_count_match is False
    assert result.develop_label_semantic_coverage is True
    assert result.comparison_policy_passed is True
    assert report.develop_label_comparison["develop_count_matches"] == 0
    assert report.develop_label_comparison["current_count_matches"] == 1
    assert report.develop_label_comparison["cases"][0]["id"] == 64
    assert report.comparison_policy["passed"] == 1


@pytest.mark.asyncio
async def test_eval_retries_oversized_query_lists_before_scoring():
    class Generator:
        def __init__(self, outputs):
            self.outputs = list(outputs)
            self.calls = []

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(content=self.outputs.pop(0))

    class Judge:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, _messages):
            return evaluator.CoverageJudgment(covered=True)

    oversized = json.dumps({"query_list": [{"query": f"month {i}"} for i in range(9)]})
    coarsened = json.dumps({"query_list": [{"query": f"quarter {i}"} for i in range(1, 5)]})
    generator = Generator([oversized, coarsened])
    case = {
        "id": 1,
        "difficulty": 2,
        "domain": "healthcare",
        "messages": [{"role": "user", "content": "How did monthly cases change over the year?"}],
        "expected_queries": {"query_list": [{"query": f"quarter {i}"} for i in range(1, 5)]},
    }

    result = await evaluator.run_case(case, "test prompt", generator, "model", Judge(), "judge")

    assert len(generator.calls) == 2
    assert "coarser contiguous intervals" in generator.calls[1][0]["content"]
    assert [query.removeprefix("quarter ") for query in result.generated_queries] == ["1", "2", "3", "4"]
    assert result.decomposition_count_match is True
    assert result.generation_fallback is None


@pytest.mark.asyncio
async def test_eval_falls_back_to_the_complete_user_query_after_oversized_retry_fails():
    class Generator:
        def __init__(self, output):
            self.output = output
            self.calls = 0

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, _messages):
            self.calls += 1
            return SimpleNamespace(content=self.output)

    class Judge:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, _messages):
            return evaluator.CoverageJudgment(covered=True)

    oversized = json.dumps({"query_list": [{"query": f"period {i}"} for i in range(9)]})
    generator = Generator(oversized)
    question = "How did monthly incidence change from January through December?"
    case = {
        "id": 2,
        "difficulty": 2,
        "domain": "healthcare",
        "messages": [{"role": "user", "content": question}],
        "expected_queries": {"query_list": [{"query": question}]},
    }

    result = await evaluator.run_case(case, "test prompt", generator, "model", Judge(), "judge")

    assert generator.calls == 2
    assert result.generated_queries == [question]
    assert result.generation_fallback is not None
    assert result.error is None


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
