"""
Prompt evaluation script for temporal filter generation (v2).

Dataset schema (temporal_filter.json):
    {
      "id": int,
      "messages": [{"role": ..., "content": ...}, ...],
      "query_with_temporal_filter": bool
    }

Pipeline output schema mirrors `openrag/components/pipeline.py` and
`eval_query_decomposition.py`: a `SearchQueries` object containing
`Query` items, each optionally carrying a `temporal_filters` list of
`TemporalPredicate` items.

Metrics:
  - filter_detection_accuracy   : (TP + TN) / N
  - filter_detection_precision  : TP / (TP + FP)
  - filter_detection_recall     : TP / (TP + FN)
  - filter_correctness          : LLM-as-judge verdict on the generated filter
                                  (evaluated only on TP cases — filter expected
                                  AND generated). Ratio of cases where the judge
                                  marks the filter correct.

Positive class = a filter IS expected / was generated.
  TP: expected=True,  generated=True
  FP: expected=False, generated=True
  FN: expected=True,  generated=False
  TN: expected=False, generated=False

Judge:
  Called only on TP cases. Receives the chat history, the generated
  `SearchQueries` JSON, and the current date. Returns a single boolean
  verdict covering the whole generated output (all sub-queries, all
  temporal filters considered together).

Usage:
    uv run python eval_temporal_filter_generation_v2.py [OPTIONS]

Options:
    --dataset PATH   Path to the dataset JSON file
                     (default: datasets/temporal_filter.json)
    --prompt PATH    Path to a specific prompt template file.
                     If omitted, all *.txt files in ./prompts/ are evaluated.
    --output PATH    Write JSON results to this file.

Required environment (candidate models — semicolon-separated):
    BASE_URLS, API_KEYS, MODELS

Optional environment (LLM-as-judge; defaults to the first candidate model
if unset):
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
    if MODELS:
        first = next(iter(MODELS.values()))
        return dict(first)
    return None


# ---------------------------------------------------------------------------
# Reference date — pinned so relative-date expressions in the dataset
# resolve deterministically across reruns. Gold labels were built against
# 2026-04-19; change this only if you regenerate the gold.
# ---------------------------------------------------------------------------

DATASET_CURRENT_DATE = datetime(2026, 4, 19).strftime("%A, %B %d, %Y")


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


class FilterJudgment(BaseModel):
    """LLM-as-judge verdict on the generated temporal filter(s)."""

    correct: bool = Field(
        description=(
            "True iff the generated temporal_filters, taken as a whole across all sub-queries, "
            "correctly represent the time constraint implied by the user's last message. "
            "For relative expressions (e.g. 'last week', 'past N days') allow reasonable "
            "interpretations. Second-level precision is not required."
        )
    )
    reasoning: str | None = Field(
        default=None,
        description="Only set when correct=false. One or two sentences naming the defect. Null when correct=true.",
    )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: int
    expected_filter: bool
    generated_filter: bool
    detection_correct: bool  # expected == generated
    judge_verdict: bool | None  # only set on TP cases
    judge_reasoning: str | None
    generated_queries: list[dict]  # [{"query": str, "temporal_filters": list | None}]
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
    # Confusion matrix on filter detection
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    # Detection metrics
    filter_detection_accuracy: float = 0.0
    filter_detection_precision: float = 0.0
    filter_detection_recall: float = 0.0
    filter_detection_f1: float = 0.0
    # Judge metrics — computed over TP cases only
    judge_total: int = 0
    judge_correct: int = 0
    filter_correctness: float = 0.0
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


def format_prompt(template: str, last_message: str, current_date: str) -> str:
    """Fill {current_date} and {query_language} placeholders."""
    try:
        from langdetect import detect  # type: ignore

        lang = detect(last_message)
    except Exception:
        lang = "en"

    return template.format(current_date=current_date, query_language=lang)


JUDGE_SYSTEM_PROMPT = """You are an impartial evaluator judging whether the temporal filters generated for a user query are correct.

You will receive:
  - The conversation history (role-tagged messages).
  - The current date the generator was working with.
  - The generator's structured output: a JSON object of the shape
        {{"query_list": [{{"query": str, "temporal_filters": [...] | null}}, ...]}}
    where each `temporal_filters` entry, when present, is a list of predicates
    of the form {{"field": "created_at", "operator": ">=|>|<=|<|==|!=", "value": "ISO 8601 UTC"}},
    AND-combined inside a single sub-query.

Judge the temporal filters as a whole across all sub-queries — not each sub-query in isolation.

Rules:
  - Correct iff the generated temporal_filters together capture the creation-date
    constraint implied by the user's last turn (and any necessary context from
    earlier turns).
  - Relative expressions ("last week", "past N days", "this month") admit
    reasonable interpretations (e.g. Monday-to-Monday vs rolling window for
    "last week"). Second-level precision is not required.
  - Open-ended recency ("past N days/weeks", "this week", "since X") is correctly
    represented by a lower-bound-only predicate — do NOT penalise a missing upper
    bound in that case.
  - Closed intervals ("yesterday", "on [date]", "in [month]", "in [year]", "between X and Y")
    require both bounds.
  - `before X` is correctly represented by a single `<` or `<=` predicate on X.
  - `since X` is correctly represented by a single `>=` or `>` predicate on X.
  - For exclusions (e.g. "last year except March"), the split into two
    sub-queries with disjoint half-open intervals is correct — a single negated
    predicate is not.
  - For multi-entity splits that share a time period, the same filter should
    appear on every sub-query.

Return JSON with:
  - correct: bool — true iff the generated temporal_filters are correct overall.
  - reasoning: only set this field when correct=false; one or two sentences
    naming the defect. When correct=true, leave reasoning null.
"""


def _format_judge_input(messages: list[dict], current_date: str, generated: SearchQueries) -> str:
    history = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    generated_json = json.dumps(
        {"query_list": [q.model_dump() for q in generated.query_list]},
        ensure_ascii=False,
        indent=2,
    )
    return f"Current date: {current_date}\n\nChat history:\n{history}\n\nGenerator output:\n{generated_json}\n"


async def judge_filter(
    messages: list[dict],
    current_date: str,
    generated: SearchQueries,
    judge: ChatOpenAI,
    judge_base_url: str,
) -> FilterJudgment:
    """Call the judge LLM to decide whether the generated filter(s) are correct."""
    prompt_messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": _format_judge_input(messages, current_date, generated)},
    ]
    judgment: FilterJudgment = await judge.bind(**_model_kwargs(judge_base_url)).ainvoke(prompt_messages)
    if judgment.correct:
        judgment.reasoning = None
    return judgment


async def run_case(
    case: dict,
    prompt_template: str,
    query_generator: ChatOpenAI,
    model_base_url: str,
    judge: ChatOpenAI,
    judge_base_url: str,
    current_date: str,
) -> CaseResult:
    """Run a single test case and return its result."""
    messages = case["messages"]
    last_message = messages[-1]["content"]
    prompt = format_prompt(prompt_template, last_message, current_date)
    llm_messages = build_llm_messages(prompt, messages)

    expected_filter: bool = bool(case["query_with_temporal_filter"])

    generated_queries: list[dict] = []
    generated_filter = False
    output: SearchQueries | None = None
    error: str | None = None

    try:
        output = await query_generator.bind(**_model_kwargs(model_base_url)).ainvoke(llm_messages)
        generated_queries = [
            {
                "query": q.query,
                "temporal_filters": (
                    [p.model_dump() for p in q.temporal_filters] if q.temporal_filters is not None else None
                ),
            }
            for q in output.query_list
        ]
        generated_filter = any(q.temporal_filters for q in output.query_list)
    except Exception as exc:
        error = f"generator: {exc}"

    detection_correct = error is None and (expected_filter == generated_filter)

    judge_verdict: bool | None = None
    judge_reasoning: str | None = None
    # Judge only on TP cases — filter expected AND generated.
    if error is None and expected_filter and generated_filter and output is not None:
        try:
            judgment = await judge_filter(messages, current_date, output, judge, judge_base_url)
            judge_verdict = judgment.correct
            judge_reasoning = judgment.reasoning
        except Exception as exc:
            error = f"judge: {exc}"

    return CaseResult(
        id=case["id"],
        expected_filter=expected_filter,
        generated_filter=generated_filter,
        detection_correct=detection_correct,
        judge_verdict=judge_verdict,
        judge_reasoning=judge_reasoning,
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
    current_date: str,
) -> ModelReport:
    """Run all dataset cases for one model, with a tqdm progress bar."""
    model_base_url = model_cfg["base_url"]
    query_generator = ChatOpenAI(
        base_url=model_base_url,
        api_key=model_cfg.get("api_key", "EMPTY"),
        model=model_cfg["model"],
        temperature=0.1,
    ).with_structured_output(SearchQueries, method="function_calling")

    judge_base_url = judge_cfg["base_url"]
    judge = ChatOpenAI(
        base_url=judge_base_url,
        api_key=judge_cfg.get("api_key", "EMPTY"),
        model=judge_cfg["model"],
        temperature=0.0,
    ).with_structured_output(FilterJudgment, method="function_calling")

    tasks = [
        run_case(case, prompt_template, query_generator, model_base_url, judge, judge_base_url, current_date)
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


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


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

    for r in results:
        report.total += 1
        if r.error:
            report.errors += 1
            continue
        if r.expected_filter and r.generated_filter:
            report.tp += 1
        elif not r.expected_filter and r.generated_filter:
            report.fp += 1
        elif r.expected_filter and not r.generated_filter:
            report.fn += 1
        else:
            report.tn += 1
        if r.judge_verdict is not None:
            report.judge_total += 1
            if r.judge_verdict:
                report.judge_correct += 1

    scored = report.total - report.errors
    report.filter_detection_accuracy = _safe_div(report.tp + report.tn, scored)
    report.filter_detection_precision = _safe_div(report.tp, report.tp + report.fp)
    report.filter_detection_recall = _safe_div(report.tp, report.tp + report.fn)
    p, rec = report.filter_detection_precision, report.filter_detection_recall
    report.filter_detection_f1 = _safe_div(2 * p * rec, p + rec)
    report.filter_correctness = _safe_div(report.judge_correct, report.judge_total)

    for r in results:
        report.cases.append(asdict(r))

    return report


def print_model_summary(report: ModelReport) -> None:
    print()
    print(f"  Model  : {report.model_name}   (judge: {report.judge_model})")
    print(f"  Total  : {report.total}   Errors: {report.errors}")
    print(f"  Confusion : TP={report.tp}  FP={report.fp}  FN={report.fn}  TN={report.tn}")
    print(f"  detection accuracy  : {report.filter_detection_accuracy:.1%}")
    print(f"  detection precision : {report.filter_detection_precision:.1%}")
    print(f"  detection recall    : {report.filter_detection_recall:.1%}")
    print(f"  detection F1        : {report.filter_detection_f1:.1%}")
    if report.judge_total:
        print(
            f"  filter correctness  : {report.judge_correct:>3}/{report.judge_total:<3} "
            f"({report.filter_correctness:.1%})    [judge called on TP only]"
        )
    else:
        print("  filter correctness  : n/a (no TP cases)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate temporal filter generation (v2): detection precision/recall + LLM-judged filter correctness on TP cases."
    )
    parser.add_argument(
        "--dataset",
        default=str(HERE / "datasets" / "temporal_filter.json"),
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

    dataset_path = Path(args.dataset).resolve()
    try:
        dataset_path.relative_to(HERE)
    except ValueError:
        print(f"Error: --dataset path must be inside {HERE}")
        return

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

    current_date = DATASET_CURRENT_DATE

    print(f"Loaded {len(dataset)} test cases from {dataset_path.name}")
    print(f"Found {len(prompt_paths)} prompt(s): {', '.join(p.name for p in prompt_paths)}")
    print(f"Evaluating {len(MODELS)} model(s): {', '.join(MODELS)}")
    print(f"Judge model : {judge_cfg['model']}")
    print(f"Current date: {current_date}")

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
                current_date=current_date,
            )
            print_model_summary(report)
            prompt_reports.append(report)

        output_prompts.append(
            {
                "prompt": prompt_rel,
                "models": [asdict(r) for r in prompt_reports],
            }
        )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "dataset": str(dataset_path.relative_to(HERE)),
            "judge_model": judge_cfg["model"],
            "current_date": current_date,
            "prompts": output_prompts,
        }
        with output_path.open("w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
