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
                     May point to a prompt anywhere inside the repository, including
                     the production template. If omitted, evaluate ./prompts/*.txt.
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
import importlib.util
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from tqdm.asyncio import tqdm

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load_production_module(module_name: str, source_path: Path) -> ModuleType:
    """Load a pure production module without importing the application packages."""
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_OPENRAG = _REPOSITORY_ROOT / "openrag"
if str(_OPENRAG) not in sys.path:
    sys.path.insert(0, str(_OPENRAG))
_QUERY_SCHEMA = _load_production_module("prompt_eval_production_query_schema", _OPENRAG / "core/models/query.py")
SearchQueries = _QUERY_SCHEMA.SearchQueries
Query = _QUERY_SCHEMA.Query
MAX_QUERY_SUBQUERIES = _QUERY_SCHEMA.MAX_QUERY_SUBQUERIES
calendar_anchors = _load_production_module(
    "prompt_eval_production_calendar_anchors", _OPENRAG / "core/prompts/calendar_anchors.py"
).calendar_anchors
QUERY_CONTEXTUALIZER_JSON_HINT = _load_production_module(
    "prompt_eval_production_contextualizer_hint", _OPENRAG / "core/prompts/query_contextualizer_hint.py"
).QUERY_CONTEXTUALIZER_JSON_HINT

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

DATASET_CURRENT_DATETIME = datetime(2026, 4, 17, tzinfo=UTC)
DATASET_CURRENT_DATE = DATASET_CURRENT_DATETIME.strftime("%A, %B %d, %Y, %H:%M:%S")


# ---------------------------------------------------------------------------
# Pydantic model — use the production query schema, including its fan-out cap
# ---------------------------------------------------------------------------


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
    comparison_policy_passed: bool | None = None
    comparison_policy_reason: str | None = None
    develop_expected_queries: list[str] | None = None
    develop_label_count_match: bool | None = None
    develop_label_semantic_coverage: bool | None = None
    develop_label_coverage_reasoning: str | None = None
    develop_label_error: str | None = None
    generation_fallback: str | None = None
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
    comparison_policy: dict = field(default_factory=dict)
    develop_label_comparison: dict = field(default_factory=dict)
    cases: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------


def build_llm_messages(prompt: str, messages: list[dict]) -> list[dict]:
    """Build the prompt messages sent by QueryService.generate_query."""
    chat_history = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
    return [
        {"role": "system", "content": prompt + QUERY_CONTEXTUALIZER_JSON_HINT},
        {"role": "user", "content": f"Here is the chat history: \n{chat_history}\n"},
    ]


def _model_kwargs(base_url: str) -> dict:
    """Return call-time kwargs; omit vLLM-specific extra_body for OpenAI endpoints."""
    kwargs: dict = {"max_completion_tokens": 1024}
    # if "openai.com" not in base_url:
    #     kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return kwargs


def format_prompt(template: str, last_message: str) -> str:
    """Render production prompt placeholders against a pinned eval date."""
    try:
        from langdetect import detect  # type: ignore

        lang = detect(last_message)
    except Exception:
        lang = "en"

    anchors = calendar_anchors(DATASET_CURRENT_DATETIME)
    current_date = DATASET_CURRENT_DATE
    if "{calendar_anchors}" not in template:
        current_date = f"{current_date}\n{anchors}"
    return template.format(
        current_date=current_date,
        query_language=lang,
        calendar_anchors=anchors,
        max_query_subqueries=MAX_QUERY_SUBQUERIES,
    )


def _json_slice(text: str) -> str:
    """Best-effort extract the first JSON object, matching QueryService."""
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _is_oversized_query_list_error(error: Exception) -> bool:
    """Recognize the production schema error caused by exceeding the fan-out cap."""
    if not isinstance(error, PydanticValidationError):
        return False
    return any(
        item.get("loc") == ("query_list",) and item.get("type") in {"too_long", "list_too_long"}
        for item in error.errors()
    )


async def _generate_queries(
    query_generator: ChatOpenAI,
    model_base_url: str,
    llm_messages: list[dict],
    last_message: str,
) -> tuple[SearchQueries, str | None]:
    """Generate and validate queries, retrying over-cap output like QueryService."""
    generator = query_generator.bind(
        **_model_kwargs(model_base_url),
        response_format={"type": "json_object"},
    )
    messages = llm_messages
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            response = await generator.ainvoke(messages)
            return SearchQueries.model_validate_json(_json_slice(str(response.content))), None
        except Exception as exc:
            last_error = exc
            if attempt == 1 and _is_oversized_query_list_error(exc):
                retry_instruction = (
                    f"The previous JSON was rejected because it contained more than "
                    f"{MAX_QUERY_SUBQUERIES} sub-queries. Regenerate the complete result within the cap. "
                    "For time ranges, group adjacent periods into coarser contiguous intervals that cover "
                    "the entire range. For other facets, group related details while retaining every item "
                    "and shared criterion. Never omit trailing periods or any item. Return only the full JSON."
                )
                messages = [
                    {**messages[0], "content": f"{messages[0]['content']}\n\n{retry_instruction}"},
                    *messages[1:],
                ]

    fallback_reason = f"query generation failed after two attempts: {last_error}"
    return SearchQueries(query_list=[Query(query=last_message)]), fallback_reason


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


def score_comparison_policy(policy: dict | None, generated: list[str]) -> tuple[bool | None, str | None]:
    """Check that comparisons use one query per item with all shared criteria."""
    if policy is None:
        return None, None

    items = policy["items"]
    criteria = policy["shared_criteria"]

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    if len(generated) != len(items):
        return False, f"Expected one query for each of {len(items)} compared items; got {len(generated)}."

    assigned_items: list[str] = []
    normalized_criteria = [normalize(criterion) for criterion in criteria]
    for index, query in enumerate(generated, start=1):
        normalized_query = normalize(query)
        matched_items = [item for item in items if normalize(item) in normalized_query]
        if len(matched_items) != 1:
            return False, f"Query {index} must name exactly one compared item; it names {len(matched_items)}."
        missing_criteria = [
            criterion
            for criterion, normalized in zip(criteria, normalized_criteria, strict=True)
            if normalized not in normalized_query
        ]
        if missing_criteria:
            return False, f"Query {index} omits shared criteria: {', '.join(missing_criteria)}."
        assigned_items.append(matched_items[0])

    if len(set(assigned_items)) != len(items):
        return False, "Each compared item must appear in exactly one query."
    return True, None


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
    generation_fallback: str | None = None
    error: str | None = None

    try:
        output, generation_fallback = await _generate_queries(
            query_generator,
            model_base_url,
            llm_messages,
            last_message,
        )
        generated_queries = [q.query for q in output.query_list]
        n_generated = len(generated_queries)
    except Exception as exc:
        error = f"generator: {exc}"

    count_match = n_generated == n_expected and error is None
    comparison_policy_passed, comparison_policy_reason = score_comparison_policy(
        case.get("comparison_policy"), generated_queries
    )

    # Semantic coverage (judge) — runs even when counts mismatch, unless generator errored fatally.
    covered = False
    coverage_reasoning: str | None = None
    develop_expected_queries = case.get("develop_expected_queries")
    develop_count_match: bool | None = None
    develop_covered: bool | None = None
    develop_coverage_reasoning: str | None = None
    develop_label_error: str | None = None
    if error is None:
        try:
            judgment = await judge_semantic_coverage(
                expected_queries, generated_queries, coverage_judge, judge_base_url
            )
            covered = judgment.covered
            coverage_reasoning = judgment.reasoning
        except Exception as exc:
            error = f"coverage_judge: {exc}"

    if develop_expected_queries is not None:
        develop_count_match = n_generated == len(develop_expected_queries) and error is None
        if error is None:
            try:
                judgment = await judge_semantic_coverage(
                    develop_expected_queries, generated_queries, coverage_judge, judge_base_url
                )
                develop_covered = judgment.covered
                develop_coverage_reasoning = judgment.reasoning
            except Exception as exc:
                develop_label_error = f"develop_label_judge: {exc}"

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
        comparison_policy_passed=comparison_policy_passed,
        comparison_policy_reason=comparison_policy_reason,
        develop_expected_queries=develop_expected_queries,
        develop_label_count_match=develop_count_match,
        develop_label_semantic_coverage=develop_covered,
        develop_label_coverage_reasoning=develop_coverage_reasoning,
        develop_label_error=develop_label_error,
        generation_fallback=generation_fallback,
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
    concurrency: int = 8,
) -> ModelReport:
    """Run all dataset cases for one model, with a tqdm progress bar."""
    model_base_url = model_cfg["base_url"]
    # Match QueryService.generate_query: JSON-object response mode, deterministic
    # extraction, and production SearchQueries validation.
    query_generator = ChatOpenAI(
        base_url=model_base_url,
        api_key=model_cfg.get("api_key", "EMPTY"),
        model=model_cfg["model"],
        temperature=0.0,
    )

    judge_base_url = judge_cfg["base_url"]
    judge_base = ChatOpenAI(
        base_url=judge_base_url,
        api_key=judge_cfg.get("api_key", "EMPTY"),
        model=judge_cfg["model"],
        temperature=0.0,
    )
    coverage_judge = judge_base.with_structured_output(CoverageJudgment, method="function_calling")

    case_semaphore = asyncio.Semaphore(concurrency)

    async def run_bounded_case(case: dict) -> CaseResult:
        async with case_semaphore:
            return await run_case(
                case,
                prompt_template,
                query_generator,
                model_base_url,
                coverage_judge,
                judge_base_url,
            )

    tasks = [run_bounded_case(case) for case in dataset]

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
        if r.error or r.develop_label_error:
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
        if r.error or r.develop_label_error:
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

    comparison_cases = [r for r in results if r.comparison_policy_passed is not None]
    if comparison_cases:
        report.comparison_policy = {
            "total": len(comparison_cases),
            "passed": sum(r.comparison_policy_passed is True for r in comparison_cases),
            "cases": [
                {
                    "id": r.id,
                    "passed": r.comparison_policy_passed,
                    "reason": r.comparison_policy_reason,
                    "generated_queries": r.generated_queries,
                }
                for r in comparison_cases
            ],
        }

    develop_labeled = [r for r in results if r.develop_expected_queries is not None]
    if develop_labeled:
        report.develop_label_comparison = {
            "total": len(develop_labeled),
            "case_ids": [r.id for r in develop_labeled],
            "develop_count_matches": sum(r.develop_label_count_match is True for r in develop_labeled),
            "current_count_matches": sum(r.decomposition_count_match for r in develop_labeled),
            "develop_semantic_coverage_passed": sum(r.develop_label_semantic_coverage is True for r in develop_labeled),
            "current_semantic_coverage_passed": sum(r.decomposition_semantic_coverage for r in develop_labeled),
            "cases": [
                {
                    "id": r.id,
                    "generated_query_count": r.n_generated_queries,
                    "develop_expected_query_count": len(r.develop_expected_queries or []),
                    "current_expected_query_count": r.n_expected_queries,
                    "develop_count_match": r.develop_label_count_match,
                    "current_count_match": r.decomposition_count_match,
                    "develop_semantic_coverage": r.develop_label_semantic_coverage,
                    "current_semantic_coverage": r.decomposition_semantic_coverage,
                }
                for r in develop_labeled
            ],
        }

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
    if report.develop_label_comparison:
        comparison = report.develop_label_comparison
        print(
            "  relabeled cases: "
            f"develop labels {comparison['develop_count_matches']}/{comparison['total']} count matches, "
            f"current labels {comparison['current_count_matches']}/{comparison['total']} count matches"
        )
        for case in comparison["cases"]:
            print(
                f"    case {case['id']}: generated {case['generated_query_count']}; "
                f"develop expected {case['develop_expected_query_count']} "
                f"({'match' if case['develop_count_match'] else 'miss'}), current expected "
                f"{case['current_expected_query_count']} "
                f"({'match' if case['current_count_match'] else 'miss'})"
            )
    if report.comparison_policy:
        comparison = report.comparison_policy
        print(
            f"  comparison policy: {comparison['passed']}/{comparison['total']} cases follow one-query-per-item structure"
        )
        for case in comparison["cases"]:
            if not case["passed"]:
                print(f"    case {case['id']}: {case['reason']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parents[2]


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
        help="Prompt template inside the repository (default: evaluate all *.txt files in ./prompts/)",
    )
    parser.add_argument("--output", default=None, help="Write JSON results to this file")
    parser.add_argument(
        "--case-ids",
        type=lambda value: [int(case_id.strip()) for case_id in value.split(",") if case_id.strip()],
        default=None,
        help="Comma-separated case IDs to evaluate (default: all cases)",
    )
    parser.add_argument("--concurrency", type=int, default=8, help="Maximum number of cases running at once")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if args.concurrency < 1:
        print("Error: --concurrency must be at least 1")
        return

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
            prompt_path.relative_to(PROJECT_ROOT)
        except ValueError:
            print(f"Error: --prompt path must be inside {PROJECT_ROOT}")
            return
        prompt_paths = [prompt_path]
    else:
        prompt_paths = sorted((HERE / "prompts").glob("*.txt"))
        if not prompt_paths:
            print(f"No prompt files found in {HERE / 'prompts'}")
            return

    with dataset_path.open() as f:
        dataset: list[dict] = json.load(f)
    if args.case_ids is not None:
        requested_ids = set(args.case_ids)
        dataset = [case for case in dataset if case["id"] in requested_ids]
        found_ids = {case["id"] for case in dataset}
        missing_ids = requested_ids - found_ids
        if missing_ids:
            print(f"Error: requested case IDs are not in the dataset: {sorted(missing_ids)}")
            return
    print(f"Loaded {len(dataset)} test cases from {dataset_path.name}")
    print(f"Found {len(prompt_paths)} prompt(s): {', '.join(p.name for p in prompt_paths)}")
    print(f"Evaluating {len(MODELS)} model(s): {', '.join(MODELS)} (concurrency {args.concurrency})")
    print(f"Judge model: {judge_cfg['model']}")

    # Run each prompt × each model
    output_prompts: list[dict] = []
    for prompt_path in prompt_paths:
        prompt_template = prompt_path.read_text()
        try:
            prompt_rel = str(prompt_path.relative_to(HERE))
        except ValueError:
            prompt_rel = str(prompt_path.relative_to(PROJECT_ROOT))
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
                concurrency=args.concurrency,
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
            "concurrency": args.concurrency,
            "prompts": output_prompts,
        }
        with output_path.open("w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
