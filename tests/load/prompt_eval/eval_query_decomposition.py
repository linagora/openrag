"""
Prompt evaluation script for query reformulation prompts.

Loads the query_decomposition dataset, runs each test case through one or more
prompt templates against every model defined in the `MODELS` dict, and scores
two decomposition metrics.

Metrics (this pass):
  1. decomposition_count_matching      — generated query count equals expected
  2. decomposition_semantic_coverage   — LLM-as-judge boolean: does the generated
                                          split, taken as a whole, semantically
                                          cover every expected sub-query? The
                                          count does not have to match exactly;
                                          only semantic coverage matters.

Usage:
    uv run python eval_query_decomposition.py [OPTIONS]

Options:
    --dataset PATH   Path to the dataset JSON file
                     (default: datasets/query_decomposition.json)
    --prompt PATH    Path to a specific prompt template file.
                     If omitted, all *.txt files in ./prompts/ are evaluated.
    --output PATH    Write JSON results to this file.

Required environment (candidate models under evaluation — semicolon-separated):
    BASE_URLS, API_KEYS, MODELS

Optional environment (LLM-as-judge for semantic coverage; defaults to the
first candidate model if unset):
    JUDGE_BASE_URL, JUDGE_API_KEY, JUDGE_MODEL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tqdm.asyncio import tqdm

load_dotenv()

# ---------------------------------------------------------------------------
# Models to evaluate — configured via .env
# ---------------------------------------------------------------------------


def _parse_env_list(key: str) -> list[str]:
    return [v for v in os.environ.get(key, "").split(";") if v.strip()]


def _build_models() -> dict[str, dict]:
    base_urls = _parse_env_list("BASE_URLS")
    api_keys = _parse_env_list("API_KEYS")
    models = _parse_env_list("MODELS")
    if not (base_urls and api_keys and models):
        return {}
    if not (len(base_urls) == len(api_keys) == len(models)):
        raise ValueError(
            f"BASE_URLS ({len(base_urls)}), API_KEYS ({len(api_keys)}), and MODELS ({len(models)}) "
            "must have the same number of semicolon-separated entries."
        )
    return {
        model: {"base_url": base_url, "api_key": api_key, "model": model}
        for base_url, api_key, model in zip(base_urls, api_keys, models)
    }


MODELS: dict[str, dict] = _build_models()


def _judge_config() -> dict | None:
    base_url = os.environ.get("JUDGE_BASE_URL")
    api_key = os.environ.get("JUDGE_API_KEY")
    model = os.environ.get("JUDGE_MODEL")
    if base_url and api_key and model:
        return {"base_url": base_url, "api_key": api_key, "model": model}
    # Fall back to the first candidate model.
    if MODELS:
        first = next(iter(MODELS.values()))
        return dict(first)
    return None


# ---------------------------------------------------------------------------
# Reference date — pinned so relative-date expressions in the dataset
# resolve deterministically across reruns. Gold labels were built against
# 2026-04-17; change this only if you regenerate the gold.
# ---------------------------------------------------------------------------

DATASET_CURRENT_DATE = datetime(2026, 4, 17).strftime("%A, %B %d, %Y, %H:%M:%S")


# ---------------------------------------------------------------------------
# Pydantic models — mirrors openrag/components/pipeline.py
# ---------------------------------------------------------------------------


class TemporalPredicate(BaseModel):
    field: Literal["created_at"] = Field(
        default="created_at",
        description="Document metadata field to filter on. Always `created_at` for now.",
    )
    operator: Literal[">", "<", ">=", "<="] = Field(
        description="Comparison operator applied to the date field.",
    )
    value: str = Field(
        description='ISO 8601 datetime with timezone, e.g. "2026-03-15T00:00:00+00:00".',
    )


class Query(BaseModel):
    query: str = Field(description="A semantically enriched, descriptive query for vector similarity search.")
    temporal_filters: list[TemporalPredicate] | None = Field(
        default=None,
        description="Date predicates on `created_at`, AND-combined. Null when no temporal reference in the query.",
    )


class SearchQueries(BaseModel):
    """Search queries for semantic retrieval."""

    query_list: list[Query] = Field(..., description="Search sub-queries to retrieve relevant documents.")


class CoverageJudgment(BaseModel):
    """LLM-as-judge output for decomposition_semantic_coverage."""

    covered: bool = Field(
        description="True if the generated sub-queries, taken as a whole, semantically cover the information need of every expected sub-query. The number of generated sub-queries does NOT have to match the expected count — only the semantic coverage matters."
    )
    reasoning: str | None = Field(
        default=None,
        description="Only set when covered=false. One or two sentences naming which expected sub-query is NOT covered by any generated sub-query. Leave null when covered=true.",
    )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: int
    difficulty: int
    domain: str
    n_expected_queries: int
    n_generated_queries: int
    decomposition_count_match: bool
    decomposition_semantic_coverage: bool
    coverage_reasoning: str | None
    expected_queries: list[str]
    generated_queries: list[str]
    error: str | None = None


@dataclass
class ModelReport:
    model_name: str
    timestamp: str
    prompt_path: str
    dataset_path: str
    judge_model: str
    total: int = 0
    errors: int = 0
    # decomposition_count_matching
    count_match_passed: int = 0
    count_match_accuracy: float = 0.0
    # decomposition_semantic_coverage
    semantic_coverage_passed: int = 0
    semantic_coverage_accuracy: float = 0.0
    by_difficulty: dict = field(default_factory=dict)
    cases: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------


def build_llm_messages(prompt: str, messages: list[dict]) -> list[dict]:
    """Build the two-message list sent to the LLM, mirroring pipeline.py."""
    chat_history = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Here is the chat history: \n{chat_history}\n"},
    ]


def _model_kwargs(base_url: str) -> dict:
    """Return call-time kwargs; omit vLLM-specific extra_body for OpenAI endpoints."""
    kwargs: dict = {"max_completion_tokens": 512}
    # if "openai.com" not in base_url:
    #     kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return kwargs


def format_prompt(template: str, last_message: str) -> str:
    """Fill {current_date} and {query_language} placeholders."""
    try:
        from langdetect import detect  # type: ignore

        lang = detect(last_message)
    except Exception:
        lang = "en"

    return template.format(
        current_date=DATASET_CURRENT_DATE,
        query_language=lang,
    )


JUDGE_SYSTEM_PROMPT = """You are an impartial evaluator judging whether a set of GENERATED sub-queries semantically covers a set of EXPECTED sub-queries.

A generated sub-query "covers" an expected sub-query when it targets the same information need: same entity/subject, same time period (if any), same dimension/aspect. Wording need not match — coverage is about retrieval intent. The generated split does NOT have to match the expected count; what matters is that every expected information need is addressed by at least one generated sub-query.

Return JSON with:
- covered: boolean — true iff EVERY expected sub-query is semantically covered by at least one generated sub-query.
- reasoning: only set this field when covered=false; give one or two sentences naming which expected sub-query is missing. When covered=true, leave reasoning null.
"""


def _format_judge_input(expected: list[str], generated: list[str]) -> str:
    exp_lines = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(expected))
    gen_lines = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(generated)) if generated else "(none)"
    return f"EXPECTED sub-queries:\n{exp_lines}\n\nGENERATED sub-queries:\n{gen_lines}\n"


async def judge_semantic_coverage(
    expected: list[str],
    generated: list[str],
    judge: ChatOpenAI,
    judge_base_url: str,
) -> CoverageJudgment:
    """Call the judge LLM to decide whether the generated split covers all expected."""
    if not expected:
        return CoverageJudgment(covered=True, reasoning=None)
    if not generated:
        return CoverageJudgment(covered=False, reasoning="No generated queries produced.")
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": _format_judge_input(expected, generated)},
    ]
    judgment: CoverageJudgment = await judge.bind(**_model_kwargs(judge_base_url)).ainvoke(messages)
    # Defensive: the judge is instructed to leave reasoning null on covered=true,
    # but enforce it here too so callers can rely on the invariant.
    if judgment.covered:
        judgment.reasoning = None
    return judgment


async def run_case(
    case: dict,
    prompt_template: str,
    query_generator: ChatOpenAI,
    model_base_url: str,
    coverage_judge: ChatOpenAI,
    judge_base_url: str,
) -> CaseResult:
    """Run a single test case and return its result."""
    messages = case["messages"]
    last_message = messages[-1]["content"]
    prompt = format_prompt(prompt_template, last_message)
    llm_messages = build_llm_messages(prompt, messages)

    expected_queries = [q["query"] for q in case["expected_queries"]["query_list"]]
    n_expected = len(expected_queries)

    generated_queries: list[str] = []
    n_generated = 0
    error: str | None = None

    try:
        output: SearchQueries = await query_generator.bind(**_model_kwargs(model_base_url)).ainvoke(llm_messages)
        generated_queries = [q.query for q in output.query_list]
        n_generated = len(generated_queries)
    except Exception as exc:
        error = f"generator: {exc}"

    count_match = n_generated == n_expected and error is None

    # Semantic coverage (judge) — runs even when counts mismatch, unless generator errored fatally.
    covered = False
    coverage_reasoning: str | None = None
    if error is None:
        try:
            judgment = await judge_semantic_coverage(
                expected_queries, generated_queries, coverage_judge, judge_base_url
            )
            covered = judgment.covered
            coverage_reasoning = judgment.reasoning
        except Exception as exc:
            error = f"coverage_judge: {exc}"

    return CaseResult(
        id=case["id"],
        difficulty=case["difficulty"],
        domain=case["domain"],
        n_expected_queries=n_expected,
        n_generated_queries=n_generated,
        decomposition_count_match=count_match,
        decomposition_semantic_coverage=covered,
        coverage_reasoning=coverage_reasoning,
        expected_queries=expected_queries,
        generated_queries=generated_queries,
        error=error,
    )


async def run_eval_for_model(
    model_name: str,
    model_cfg: dict,
    judge_cfg: dict,
    dataset: list[dict],
    prompt_template: str,
    prompt_path: str,
    dataset_path: str,
) -> ModelReport:
    """Run all dataset cases for one model, with a tqdm progress bar."""
    model_base_url = model_cfg["base_url"]
    # Use function_calling (the LangChain default) so that the Pydantic schema is
    # passed to the model as a tool definition. json_mode only forces "some JSON"
    # and relies on the prompt to describe the schema — v0-style prompts that do
    # not prescribe the output shape would otherwise fail every case.
    query_generator = ChatOpenAI(
        base_url=model_base_url,
        api_key=model_cfg.get("api_key", "EMPTY"),
        model=model_cfg["model"],
        temperature=0.1,
    ).with_structured_output(SearchQueries, method="function_calling")

    judge_base_url = judge_cfg["base_url"]
    judge_base = ChatOpenAI(
        base_url=judge_base_url,
        api_key=judge_cfg.get("api_key", "EMPTY"),
        model=judge_cfg["model"],
        temperature=0.0,
    )
    coverage_judge = judge_base.with_structured_output(CoverageJudgment, method="function_calling")

    tasks = [
        run_case(
            case,
            prompt_template,
            query_generator,
            model_base_url,
            coverage_judge,
            judge_base_url,
        )
        for case in dataset
    ]

    results: list[CaseResult] = []
    for coro in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc=f"{model_name}",
        unit="case",
        leave=True,
    ):
        results.append(await coro)

    return build_model_report(results, model_name, prompt_path, dataset_path, judge_cfg["model"])


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_model_report(
    results: list[CaseResult],
    model_name: str,
    prompt_path: str,
    dataset_path: str,
    judge_model: str,
) -> ModelReport:
    report = ModelReport(
        model_name=model_name,
        timestamp=datetime.now().isoformat(),
        prompt_path=prompt_path,
        dataset_path=dataset_path,
        judge_model=judge_model,
    )

    by_diff: dict[int, dict] = {}

    for r in results:
        report.total += 1
        if r.error:
            report.errors += 1
        if r.decomposition_count_match:
            report.count_match_passed += 1
        if r.decomposition_semantic_coverage:
            report.semantic_coverage_passed += 1

        bucket = by_diff.setdefault(
            r.difficulty,
            {"total": 0, "errors": 0, "count_match_passed": 0, "semantic_coverage_passed": 0},
        )
        bucket["total"] += 1
        if r.error:
            bucket["errors"] += 1
        if r.decomposition_count_match:
            bucket["count_match_passed"] += 1
        if r.decomposition_semantic_coverage:
            bucket["semantic_coverage_passed"] += 1

    report.count_match_accuracy = report.count_match_passed / report.total if report.total else 0.0
    report.semantic_coverage_accuracy = report.semantic_coverage_passed / report.total if report.total else 0.0

    for bucket in by_diff.values():
        total = bucket["total"] or 1
        bucket["count_match_accuracy"] = bucket["count_match_passed"] / total
        bucket["semantic_coverage_accuracy"] = bucket["semantic_coverage_passed"] / total
    report.by_difficulty = {str(k): v for k, v in sorted(by_diff.items())}

    for r in results:
        report.cases.append(asdict(r))

    return report


def print_model_summary(report: ModelReport) -> None:
    print()
    print(f"  Model  : {report.model_name}   (judge: {report.judge_model})")
    print(f"  Total  : {report.total}   Errors: {report.errors}")
    print(
        f"  count_match        : {report.count_match_passed:>3}/{report.total:<3} ({report.count_match_accuracy:.1%})"
    )
    print(
        f"  semantic_coverage  : {report.semantic_coverage_passed:>3}/{report.total:<3} "
        f"({report.semantic_coverage_accuracy:.1%})"
    )
    for diff, stats in report.by_difficulty.items():
        err_tag = f"  [{stats['errors']} errors]" if stats["errors"] else ""
        print(
            f"    D{diff}: count_match {stats['count_match_passed']:>3}/{stats['total']:<3} "
            f"({stats['count_match_accuracy']:.1%})  |  "
            f"coverage {stats['semantic_coverage_passed']:>3}/{stats['total']:<3} "
            f"({stats['semantic_coverage_accuracy']:.1%})"
            f"{err_tag}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate query_decomposition prompts (decomposition count matching + semantic coverage)."
    )
    parser.add_argument(
        "--dataset",
        default=str(HERE / "datasets" / "query_decomposition.json"),
        help="Path to the dataset JSON file",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Path to a specific prompt template file (default: evaluate all *.txt files in ./prompts/)",
    )
    parser.add_argument("--output", default=None, help="Write JSON results to this file")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not MODELS:
        print("No models configured. Set BASE_URLS/API_KEYS/MODELS in the environment.")
        return

    judge_cfg = _judge_config()
    if not judge_cfg:
        print("No judge model configured (JUDGE_BASE_URL/JUDGE_API_KEY/JUDGE_MODEL) and no fallback available.")
        return

    # Validate model configs before doing any work
    errors = []
    for name, cfg in MODELS.items():
        for key in ("base_url", "model", "api_key"):
            if not cfg.get(key):
                errors.append(f"  [{name}] missing or None: '{key}'")
    if errors:
        print("Invalid model configuration:")
        for e in errors:
            print(e)
        return

    # Resolve dataset path and enforce it stays within the benchmark directory
    dataset_path = Path(args.dataset).resolve()
    try:
        dataset_path.relative_to(HERE)
    except ValueError:
        print(f"Error: --dataset path must be inside {HERE}")
        return

    # Resolve prompt path(s)
    if args.prompt:
        prompt_path = Path(args.prompt).resolve()
        try:
            prompt_path.relative_to(HERE)
        except ValueError:
            print(f"Error: --prompt path must be inside {HERE}")
            return
        prompt_paths = [prompt_path]
    else:
        prompt_paths = sorted((HERE / "prompts").glob("*.txt"))
        if not prompt_paths:
            print(f"No prompt files found in {HERE / 'prompts'}")
            return

    with dataset_path.open() as f:
        dataset: list[dict] = json.load(f)
    print(f"Loaded {len(dataset)} test cases from {dataset_path.name}")
    print(f"Found {len(prompt_paths)} prompt(s): {', '.join(p.name for p in prompt_paths)}")
    print(f"Evaluating {len(MODELS)} model(s): {', '.join(MODELS)}")
    print(f"Judge model: {judge_cfg['model']}")

    # Run each prompt × each model
    output_prompts: list[dict] = []
    for prompt_path in prompt_paths:
        prompt_template = prompt_path.read_text()
        prompt_rel = str(prompt_path.relative_to(HERE))
        sep = "-" * 72
        print(f"\n{sep}")
        print(f"PROMPT: {prompt_path.name}")
        print(sep)

        prompt_reports: list[ModelReport] = []
        for model_name, model_cfg in MODELS.items():
            report = await run_eval_for_model(
                model_name=model_name,
                model_cfg=model_cfg,
                judge_cfg=judge_cfg,
                dataset=dataset,
                prompt_template=prompt_template,
                prompt_path=prompt_rel,
                dataset_path=str(dataset_path.relative_to(HERE)),
            )
            print_model_summary(report)
            prompt_reports.append(report)

        output_prompts.append(
            {
                "prompt": prompt_rel,
                "models": [asdict(r) for r in prompt_reports],
            }
        )

    # Optionally persist results
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "dataset": str(dataset_path.relative_to(HERE)),
            "judge_model": judge_cfg["model"],
            "prompts": output_prompts,
        }
        with output_path.open("w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
