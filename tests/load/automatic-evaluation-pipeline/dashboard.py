"""Streamlit dashboard for the RAG evaluation pipeline.

Run:
    streamlit run dashboard.py

Browse past runs and trigger new ones from the sidebar.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from config import CONFIG

TREND_METRIC_GROUPS: dict[str, list[tuple[str, str, str]]] = {
    "Retrieval": [
        ("retrieval", "hit_rate", "Hit Rate"),
        ("retrieval", "mrr", "MRR"),
        ("retrieval", "recall", "Recall"),
        ("retrieval", "map", "MAP"),
        ("retrieval", "ndcg_at_5", "nDCG@5"),
        ("retrieval", "ndcg_at_10", "nDCG@10"),
        ("retrieval", "precision_at_5", "Precision@5"),
        ("retrieval", "precision_at_10", "Precision@10"),
    ],
    "Generation": [
        ("generation", "rouge1", "ROUGE-1"),
        ("generation", "rouge2", "ROUGE-2"),
        ("generation", "rougeL", "ROUGE-L"),
        ("generation", "bleu", "BLEU"),
        ("generation", "meteor", "METEOR"),
    ],
    "Judge & Faithfulness": [
        ("judge_scores", "completion_mean", "Judge Completion"),
        ("judge_scores", "precision_mean", "Judge Precision"),
        ("faithfulness", "mean_per_question", "Faithfulness (mean)"),
        ("faithfulness", "micro_ratio", "Faithfulness (micro)"),
    ],
    "Relevancy (reference-free)": [
        ("answer_relevancy", "mean_per_question", "Answer Relevancy (mean)"),
        ("answer_relevancy", "micro_ratio", "Answer Relevancy (micro)"),
        ("context_relevancy", "mean_per_question", "Context Relevancy (mean)"),
        ("context_relevancy", "micro_ratio", "Context Relevancy (micro)"),
    ],
    "Refusal": [
        ("refusal", "abstention_rate", "Abstention rate"),
        ("refusal", "false_refusal_rate", "False-refusal rate"),
    ],
    "Latency (s)": [
        ("latency", "mean", "Mean"),
        ("latency", "p50", "p50"),
        ("latency", "p95", "p95"),
        ("latency", "p99", "p99"),
    ],
    "Logprobs": [
        ("logprobs", "mean_logprob_mean", "Mean logprob"),
        ("logprobs", "perplexity_mean", "Mean perplexity"),
    ],
    "Calibration": [
        ("calibration", "spearman_completion_perplexity", "Spearman comp↔ppl"),
        ("calibration", "spearman_precision_perplexity", "Spearman prec↔ppl"),
        ("calibration", "pearson_completion_perplexity", "Pearson comp↔ppl"),
        ("calibration", "pearson_precision_perplexity", "Pearson prec↔ppl"),
    ],
}

REPO_DIR = Path(__file__).resolve().parent
REPORTS_DIR = REPO_DIR / "reports"
DATASET_PATH = REPO_DIR / "dataset.json"
GOLDEN_UPLOAD_DIR = REPO_DIR / "golden_uploads"
BENCHMARK_SCRIPT = REPO_DIR / "benchmark.py"
GENERATE_SCRIPT = REPO_DIR / "generate_questions.py"
ORCHESTRATOR_SCRIPT = REPO_DIR / "orchestrator.py"
LOG_DIR = REPORTS_DIR / ".logs"
DEPLOY_INPUTS_DIR = REPORTS_DIR / ".deploy_inputs"
SCORE_CSV_PATH = REPORTS_DIR / "score.csv"
# The separate OpenRAG clone the orchestrator checks out versions in. Sits at the
# main repo root (this pipeline lives 3 levels down under tests/load/). Computed so
# it stays correct if the pipeline dir moves; users can override it in the UI.
DEFAULT_VERSIONS_REPO = str(REPO_DIR.parent.parent.parent / "openrag-versions")
CONTEXT_ABLATION_CSV_PATH = REPORTS_DIR / "context_ablation.csv"


def quick_validate_dataset_file(path: Path) -> tuple[bool, str, dict]:
    """Lightweight golden-dataset check that mirrors benchmark._load_and_validate_dataset.

    Returns (ok, message, stats). Stats: {n_total, n_answerable, n_unanswerable, n_with_chunks}.
    Heavy validation still runs inside benchmark.py at execution time.
    """
    if not path.exists():
        return False, f"File not found: {path}", {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Could not parse JSON: {e}", {}
    if not isinstance(data, list):
        return False, "Dataset must be a JSON list of entries.", {}

    n_with_chunks = n_answerable = n_unanswerable = 0
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            return False, f"Entry #{i} is not an object.", {}
        if not isinstance(row.get("question"), str) or not row["question"].strip():
            return False, f"Entry #{i} is missing a non-empty 'question'.", {}
        if not isinstance(row.get("llm_answer"), str):
            return False, f"Entry #{i} is missing 'llm_answer'.", {}
        chunks = row.get("chunks") or []
        if not isinstance(chunks, list):
            return False, f"Entry #{i} 'chunks' must be a list.", {}
        for j, c in enumerate(chunks):
            if not isinstance(c, dict) or "id" not in c:
                return False, f"Entry #{i} chunk #{j} must have an 'id' field.", {}
        if chunks:
            n_with_chunks += 1
        if bool(row.get("answerable", True)):
            n_answerable += 1
        else:
            n_unanswerable += 1

    stats = {
        "n_total": len(data),
        "n_answerable": n_answerable,
        "n_unanswerable": n_unanswerable,
        "n_with_chunks": n_with_chunks,
    }
    msg = (
        f"{stats['n_total']} entries · {stats['n_answerable']} answerable · "
        f"{stats['n_unanswerable']} unanswerable · {stats['n_with_chunks']} with chunks"
    )
    if stats["n_with_chunks"] == 0:
        msg += " (retrieval metrics will be skipped)"
    return True, msg, stats


def list_runs() -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    # Approach B: each orchestrated run is one reports/<uuid>/ folder. Legacy flat
    # eval_*.json files (pre-orchestrator runs) are still discovered for back-compat.
    # ``dir`` is the folder a run's artifacts (CSVs, logprobs JSONL) resolve against.
    json_paths = list(REPORTS_DIR.glob("*/eval_*.json")) + list(REPORTS_DIR.glob("eval_*.json"))
    runs = []
    for json_path in json_paths:
        try:
            with open(json_path, encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        runs.append(
            {
                "path": json_path,
                "dir": json_path.parent,
                "summary": summary,
                "ts": summary.get("timestamp", json_path.stem),
            }
        )
    runs.sort(key=lambda r: r["ts"], reverse=True)
    return runs


def run_label(r: dict) -> str:
    """Dropdown label for a run: timestamp, version (if orchestrated), partition.

    A short run_id suffix keeps labels unique when two runs share a timestamp
    (e.g. the same version evaluated twice), so none get dropped from the selectbox.
    """
    cfg = r["summary"].get("config", {})
    version = cfg.get("version")
    partition = cfg.get("partition", "?")
    rid = cfg.get("run_id")
    head = f"{version} · {partition}" if version else partition
    label = f"{r['ts']} — {head}"
    if rid:
        label += f" [{rid[:6]}]"
    return label


def fmt(value, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value) -> str:
    """0.92 -> '92%'. Returns 'n/a' for missing values."""
    if value is None:
        return "n/a"
    return f"{value * 100:.0f}%"


def _tail(path: Path, n: int, block: int = 65536) -> list[str]:
    """Last n lines of a file without reading the whole thing: seek from the end in
    ~block-sized chunks until more than n newlines are collected, then slice. Cost is
    O(n * line length), not O(filesize) — matters because the sidebar re-reads run
    logs on every Streamlit rerun and those logs can grow to hundreds of MB."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-n:]


def _render_retrieval_metrics(d: dict) -> None:
    """8 retrieval metrics in a 4+4 grid (shared by summary + single-query views)."""
    cols = st.columns(4)
    cols[0].metric("Hit Rate", fmt(d.get("hit_rate")))
    cols[1].metric("MRR", fmt(d.get("mrr")))
    cols[2].metric("Recall", fmt(d.get("recall")))
    cols[3].metric("MAP", fmt(d.get("map")))
    cols = st.columns(4)
    cols[0].metric("Precision@5", fmt(d.get("precision_at_5")))
    cols[1].metric("Precision@10", fmt(d.get("precision_at_10")))
    cols[2].metric("nDCG@5", fmt(d.get("ndcg_at_5")))
    cols[3].metric("nDCG@10", fmt(d.get("ndcg_at_10")))


def _render_generation_metrics(d: dict) -> None:
    """5 n-gram overlap metrics (shared by summary + single-query views)."""
    cols = st.columns(5)
    cols[0].metric("ROUGE-1", fmt(d.get("rouge1")))
    cols[1].metric("ROUGE-2", fmt(d.get("rouge2")))
    cols[2].metric("ROUGE-L", fmt(d.get("rougeL")))
    cols[3].metric("BLEU", fmt(d.get("bleu")))
    cols[4].metric("METEOR", fmt(d.get("meteor")))


def classify(value, good: float, ok: float, higher_is_better: bool = True) -> tuple[str, str]:
    """Map a value to a (badge, word) traffic-light verdict against two thresholds.

    higher_is_better=True  -> value >= good is 🟢, >= ok is 🟡, else 🔴.
    higher_is_better=False -> value <= good is 🟢, <= ok is 🟡, else 🔴 (e.g. latency).
    Missing values render as a neutral grey badge.
    """
    if value is None:
        return "⚪", "not measured"
    if higher_is_better:
        if value >= good:
            return "🟢", "Good"
        if value >= ok:
            return "🟡", "Fair"
        return "🔴", "Poor"
    else:
        if value <= good:
            return "🟢", "Good"
        if value <= ok:
            return "🟡", "Fair"
        return "🔴", "Poor"


def _trend_arrow(cur, prev, higher_is_better: bool = True) -> str:
    """Compare against the previous run and return a coloured arrow, or '' if n/a."""
    if cur is None or prev is None:
        return ""
    delta = cur - prev
    if abs(delta) < 1e-9:
        return "→ same"
    up = delta > 0
    improving = up if higher_is_better else not up
    arrow = "↑" if up else "↓"
    word = "better" if improving else "worse"
    badge = "🟢" if improving else "🔴"
    return f"{badge} {arrow} {word}"


def build_scorecard_rows(summary: dict) -> list[dict]:
    """Translate the raw summary JSON into the plain-English health rows.

    Each row groups one representative metric under a question a non-technical
    reader actually has. Nothing is recomputed — this only reads existing fields.
    """
    retrieval = summary.get("retrieval") or {}
    faith = summary.get("faithfulness") or {}
    refusal = summary.get("refusal") or {}
    latency = summary.get("latency") or {}
    labels = summary.get("labels") or {}

    total_labeled = sum(labels.values()) if labels else 0
    fully_correct = labels.get("Fully Correct", 0)
    fully_frac = (fully_correct / total_labeled) if total_labeled else None

    hit = retrieval.get("hit_rate")
    faith_val = faith.get("mean_per_question")
    n_unanswerable = refusal.get("unanswerable_total") or 0
    abst = refusal.get("abstention_rate") if n_unanswerable else None
    lat = latency.get("mean")

    rows = [
        {
            "key": "retrieval",
            "name": "Retrieval Accuracy",
            "value": hit,
            "grade": _pct(hit),
            "badge_word": classify(hit, good=0.85, ok=0.65),
            "meaning": (
                f"The correct document was retrieved {_pct(hit)} of the time."
                if hit is not None
                else "Not measured — this dataset has no ground-truth documents."
            ),
        },
        {
            "key": "correctness",
            "name": "Answer Correctness",
            "value": fully_frac,
            "grade": _pct(fully_frac),
            "badge_word": classify(fully_frac, good=0.60, ok=0.35),
            "meaning": (
                f"{_pct(fully_frac)} of answers were fully correct "
                f"({fully_correct}/{total_labeled}); the rest were incomplete or wrong."
                if fully_frac is not None
                else "Not measured — no labelled answers in this run."
            ),
        },
        {
            "key": "faithfulness",
            "name": "Faithfulness",
            "value": faith_val,
            "grade": _pct(faith_val),
            "badge_word": classify(faith_val, good=0.90, ok=0.75),
            "meaning": (
                f"{_pct(faith_val)} of the answer's statements were backed by the "
                "source documents (the rest may be invented)."
                if faith_val is not None
                else "Not measured."
            ),
        },
        {
            "key": "abstention",
            "name": "Abstention",
            "value": abst,
            "grade": _pct(abst),
            "badge_word": classify(abst, good=0.80, ok=0.50),
            "meaning": (
                f"Correctly declined {_pct(abst)} of the {n_unanswerable} questions "
                "that had no answer in the documents."
                if abst is not None
                else "Not measured — this dataset has no unanswerable questions."
            ),
        },
        {
            "key": "latency",
            "name": "Latency",
            "value": lat,
            "grade": f"{lat:.0f}s" if lat is not None else "n/a",
            "badge_word": classify(lat, good=10, ok=25, higher_is_better=False),
            "meaning": (
                f"Average response time was {lat:.0f} seconds per question."
                if lat is not None
                else "Not measured."
            ),
            "higher_is_better": False,
        },
    ]
    return rows


def render_scorecard(summary: dict, prev_summary: dict | None = None) -> None:
    """Plain-English health summary: an overall verdict, traffic-light rows, and a
    narrative — all derived from the same numbers shown in the detailed panels below."""
    rows = build_scorecard_rows(summary)
    measured = [r for r in rows if r["value"] is not None]
    badges = [r["badge_word"][0] for r in measured]

    if "🔴" in badges:
        verdict = "⚠️ Needs work"
    elif "🟡" in badges:
        verdict = "🟡 Fair"
    elif measured:
        verdict = "✅ Healthy"
    else:
        verdict = "—"

    cfg = summary.get("config", {})
    st.subheader(f"Health summary —  {verdict}")
    st.caption(
        f"Plain-English read of this run · {cfg.get('dataset_size', '?')} questions · "
        "traffic lights: 🟢 good · 🟡 fair · 🔴 needs attention"
    )

    for r in rows:
        badge, _word = r["badge_word"]
        higher = r.get("higher_is_better", True)
        prev_val = None
        if prev_summary is not None:
            prev_rows = {pr["key"]: pr for pr in build_scorecard_rows(prev_summary)}
            prev_val = prev_rows.get(r["key"], {}).get("value")
        trend = _trend_arrow(r["value"], prev_val, higher_is_better=higher)

        cols = st.columns([4, 1, 6])
        cols[0].markdown(f"{badge} **{r['name']}**")
        cols[1].markdown(f"**{r['grade']}**")
        meaning = r["meaning"]
        if trend:
            meaning += f"  ·  _vs previous run: {trend}_"
        cols[2].caption(meaning)

    # Narrative paragraph — strengths and weaknesses, stitched from the same verdicts.
    strengths = [r["name"].lower() for r in measured if r["badge_word"][0] == "🟢"]
    issues = [r["name"].lower() for r in measured if r["badge_word"][0] == "🔴"]
    watch = [r["name"].lower() for r in measured if r["badge_word"][0] == "🟡"]

    if strengths:
        st.success("**Strengths:** " + "; ".join(strengths) + ".")
    if issues:
        st.error("**Needs attention:** " + "; ".join(issues) + ".")
    if watch:
        st.warning("**Watch:** " + "; ".join(watch) + ".")

    st.divider()


def render_breakdowns(summary: dict) -> None:
    """Per-dimension answerable-score breakdowns (typed-question diversity).

    Renders mean judge/relevancy/nDCG scores grouped by question_type and by
    difficulty, so quality can be read per slice (e.g. multi-hop vs single-hop)
    instead of as one blended number. Hidden for legacy datasets whose rows only
    carry the "unknown" bucket.
    """
    breakdowns = summary.get("breakdowns") or {}
    by_type = {k: v for k, v in (breakdowns.get("by_question_type") or {}).items() if k != "unknown"}
    by_diff = {k: v for k, v in (breakdowns.get("by_difficulty") or {}).items() if k != "unknown"}
    if not by_type and not by_diff:
        return

    st.subheader("Score breakdown by question type / difficulty")
    st.caption(
        "Mean scores over answerable rows, grouped by the generator's typed-question metadata. "
        "Completion / Precision are 1–10; Answer rel. / nDCG are 0–1 (see table)."
    )

    metric_cols = {
        "completion_mean": "Completion",
        "precision_mean": "Precision",
        "answer_relevancy_mean": "Answer rel.",
        "ndcg_mean": "nDCG",
    }

    def _table(group: dict) -> pd.DataFrame:
        df = pd.DataFrame.from_dict(group, orient="index")
        if df.empty:
            return df
        ordered = ["n"] + [c for c in metric_cols if c in df.columns]
        return df[[c for c in ordered if c in df.columns]].rename(columns=metric_cols)

    for title, group in (("By question type", by_type), ("By difficulty", by_diff)):
        if not group:
            continue
        st.markdown(f"**{title}**")
        df = _table(group)
        st.dataframe(df, use_container_width=True)
        # Faceted bar of the two 1–10 judge means (shared scale; the 0–1 ratios
        # live in the table to avoid a misleading mixed-scale chart).
        judge_df = df[[c for c in ("Completion", "Precision") if c in df.columns]]
        if not judge_df.empty:
            long = (
                judge_df.reset_index()
                .melt(id_vars="index", var_name="metric", value_name="score")
                .rename(columns={"index": "category"})
                .dropna(subset=["score"])
            )
            if not long.empty:
                chart = (
                    alt.Chart(long)
                    .mark_bar()
                    .encode(
                        x=alt.X("metric:N", title=None, axis=alt.Axis(labelAngle=0)),
                        y=alt.Y("score:Q", scale=alt.Scale(domain=[0, 10]), title="mean (1–10)"),
                        color=alt.Color("metric:N", legend=None),
                        column=alt.Column("category:N", title=None),
                        tooltip=["category", "metric", "score"],
                    )
                    .properties(height=200, width=90)
                )
                st.altair_chart(chart)


def render_summary(summary: dict) -> None:
    cfg = summary.get("config", {})
    dataset_path = cfg.get("dataset_path")
    dataset_name = Path(dataset_path).name if dataset_path else "?"

    # Version-comparison identity (orchestrated runs only).
    version = cfg.get("version")
    if version:
        commit = cfg.get("git_commit") or ""
        run_id = cfg.get("run_id") or ""
        st.caption(
            f"Version: **{version}**"
            + (f"  ·  commit `{commit[:10]}`" if commit else "")
            + (f"  ·  run `{run_id[:8]}`" if run_id else "")
        )

    st.caption(
        f"Partition: **{cfg.get('partition', '?')}**  ·  "
        f"Dataset: **{dataset_name}** ({cfg.get('dataset_size', '?')} entries)  ·  "
        f"Base URL: `{cfg.get('base_url', '?')}`  ·  "
        f"Label lang: **{cfg.get('label_language', '?')}**"
    )

    retrieval = summary.get("retrieval", {})
    st.subheader("Retrieval")
    if retrieval.get("hit_rate") is None:
        st.info("Retrieval metrics: n/a — dataset has no ground-truth chunks.")
    else:
        _render_retrieval_metrics(retrieval)

    generation = summary.get("generation", {})
    st.subheader("Generation (n-gram overlap)")
    _render_generation_metrics(generation)

    logprobs = summary.get("logprobs", {})
    if logprobs.get("n_scored"):
        st.subheader("Response confidence (logprobs)")
        cols = st.columns(4)
        cols[0].metric(
            "Mean logprob",
            fmt(logprobs.get("mean_logprob_mean"), digits=4),
            help=f"n={logprobs.get('n_scored', 0)}. Closer to 0 = more confident.",
        )
        cols[1].metric("Median logprob", fmt(logprobs.get("mean_logprob_median"), digits=4))
        cols[2].metric("Mean perplexity", fmt(logprobs.get("perplexity_mean")))
        cols[3].metric("Median perplexity", fmt(logprobs.get("perplexity_median")))

    judge = summary.get("judge_scores", {})
    st.subheader("LLM Judge")
    cols = st.columns(2)
    cols[0].metric("Completion (mean 1-10)", fmt(judge.get("completion_mean")))
    cols[1].metric("Precision (mean 1-10)", fmt(judge.get("precision_mean")))

    comp_dist = judge.get("completion_distribution", {}) or {}
    prec_dist = judge.get("precision_distribution", {}) or {}
    if comp_dist or prec_dist:
        dist_df = pd.DataFrame(
            {
                "Completion": pd.Series({int(k): int(v) for k, v in comp_dist.items()}),
                "Precision": pd.Series({int(k): int(v) for k, v in prec_dist.items()}),
            }
        ).fillna(0).astype(int).sort_index()
        dist_df.index.name = "Score"
        st.bar_chart(dist_df)

    render_breakdowns(summary)

    faith = summary.get("faithfulness", {})
    st.subheader("Faithfulness")
    cols = st.columns(3)
    cols[0].metric("Mean per question", fmt(faith.get("mean_per_question")))
    cols[1].metric("Supported claims", f"{faith.get('supported_claims', 0)} / {faith.get('total_claims', 0)}")
    cols[2].metric("Micro ratio", fmt(faith.get("micro_ratio")))

    answer_rel = summary.get("answer_relevancy", {})
    context_rel = summary.get("context_relevancy", {})
    if answer_rel or context_rel:
        st.subheader("Relevancy (reference-free)")
        cols = st.columns(4)
        cols[0].metric(
            "Answer relevancy (mean)",
            fmt(answer_rel.get("mean_per_question")),
            help=(
                f"n={answer_rel.get('n_scored', 0)} answerable rows · "
                f"{answer_rel.get('relevant_statements', 0)} / {answer_rel.get('total_statements', 0)} statements relevant. "
                "Reference-free: answer statements on-topic vs the question."
            ),
        )
        cols[1].metric("Answer relevancy (micro)", fmt(answer_rel.get("micro_ratio")))
        cols[2].metric(
            "Context relevancy (mean)",
            fmt(context_rel.get("mean_per_question")),
            help=(
                f"sampled n={context_rel.get('n_scored', 0)} "
                f"({(context_rel.get('sample_fraction') or 0) * 100:.0f}%) · "
                f"{context_rel.get('relevant_statements', 0)} / {context_rel.get('total_statements', 0)} statements relevant. "
                "Reference-free retriever signal-to-noise vs the question."
            ),
        )
        cols[3].metric("Context relevancy (micro)", fmt(context_rel.get("micro_ratio")))

    refusal = summary.get("refusal", {})
    st.subheader("Refusal / Abstention")
    cols = st.columns(3)
    cols[0].metric(
        "Abstention (unanswerable)",
        fmt(refusal.get("abstention_rate")),
        help=f"{refusal.get('abstention_count', 0)} / {refusal.get('abstention_denom', 0)}",
    )
    cols[1].metric(
        "False refusal (answerable)",
        fmt(refusal.get("false_refusal_rate")),
        help=f"{refusal.get('false_refusal_count', 0)} / {refusal.get('false_refusal_denom', 0)}",
    )
    cols[2].metric("Unanswerable in dataset", refusal.get("unanswerable_total", 0))

    labels = summary.get("labels", {}) or {}
    if labels:
        st.subheader("Response labels")
        label_df = pd.DataFrame({"count": labels}).sort_values("count", ascending=False)
        cols = st.columns([2, 3])
        cols[0].dataframe(label_df, use_container_width=True)
        cols[1].bar_chart(label_df)

    latency = summary.get("latency", {})
    st.subheader("OpenRAG latency (seconds)")
    cols = st.columns(4)
    cols[0].metric("Mean", fmt(latency.get("mean")))
    cols[1].metric("p50", fmt(latency.get("p50")))
    cols[2].metric("p95", fmt(latency.get("p95")))
    cols[3].metric("p99", fmt(latency.get("p99")))

    cot = summary.get("cot_audit", {})
    if cot.get("sample_size"):
        st.subheader(f"CoT audit ({cot.get('valid_size', 0)}/{cot.get('sample_size', 0)} valid)")
        cols = st.columns(2)
        cols[0].metric("CoT completion mean", fmt(cot.get("completion_mean")))
        cols[1].metric("CoT precision mean", fmt(cot.get("precision_mean")))

    calib = summary.get("calibration", {}) or {}
    if calib.get("n_rows"):
        st.subheader("Judge agreement / Calibration")
        fc_total = calib.get("fully_correct_total", 0)
        fc_low = calib.get("fully_correct_with_low_score", 0)
        contra_total = calib.get("contradictory_total", 0)
        contra_high = calib.get("contradictory_with_high_score", 0)
        cols = st.columns(4)
        cols[0].metric(
            "Rows scored",
            calib.get("n_rows", 0),
            help="Rows with valid label + completion + precision judges.",
        )
        cols[1].metric(
            "'Fully Correct' w/ low score",
            f"{fc_low}/{fc_total}" if fc_total else "—",
            help="Label = Fully Correct but completion or precision <7. Loud disagreement.",
        )
        cols[2].metric(
            "'Contradictory' w/ high score",
            f"{contra_high}/{contra_total}" if contra_total else "—",
            help="Label = Contradictory but completion AND precision ≥7. Loud disagreement.",
        )
        cols[3].metric(
            "Spearman comp↔ppl",
            fmt(calib.get("spearman_completion_perplexity")),
            help="Negative = more confident answers score higher (expected sign).",
        )

        bucket_cols = ["1-4", "5-6", "7-8", "9-10"]
        label_vs_comp = calib.get("label_vs_completion") or {}
        label_vs_prec = calib.get("label_vs_precision") or {}
        if label_vs_comp or label_vs_prec:
            xcols = st.columns(2)
            if label_vs_comp:
                comp_df = pd.DataFrame.from_dict(label_vs_comp, orient="index")
                comp_df = comp_df.reindex(columns=bucket_cols, fill_value=0)
                xcols[0].caption("Label × completion bucket")
                xcols[0].dataframe(comp_df, use_container_width=True)
            if label_vs_prec:
                prec_df = pd.DataFrame.from_dict(label_vs_prec, orient="index")
                prec_df = prec_df.reindex(columns=bucket_cols, fill_value=0)
                xcols[1].caption("Label × precision bucket")
                xcols[1].dataframe(prec_df, use_container_width=True)

        ppl_by_label = calib.get("perplexity_by_label") or {}
        if ppl_by_label:
            ppl_df = pd.Series(ppl_by_label, name="Mean perplexity").to_frame()
            ccols = st.columns([2, 3])
            ccols[0].caption("Mean perplexity per label")
            ccols[0].dataframe(ppl_df, use_container_width=True)
            ccols[1].caption("Score ↔ perplexity correlations")
            corr_rows = {
                "Pearson  comp↔ppl": calib.get("pearson_completion_perplexity"),
                "Spearman comp↔ppl": calib.get("spearman_completion_perplexity"),
                "Pearson  prec↔ppl": calib.get("pearson_precision_perplexity"),
                "Spearman prec↔ppl": calib.get("spearman_precision_perplexity"),
            }
            ccols[1].dataframe(
                pd.Series({k: v for k, v in corr_rows.items() if v is not None}, name="r").to_frame(),
                use_container_width=True,
            )


def render_artifacts(summary: dict, run_dir: Path = REPORTS_DIR) -> None:
    # ``run_dir`` is the run's own folder (reports/<uuid>/ for orchestrated runs, or
    # REPORTS_DIR for legacy flat runs). Artifact filenames in the summary are bare,
    # so they resolve against the report they were written next to.
    artifacts = summary.get("artifacts", {})

    per_q = artifacts.get("per_question_csv")
    if per_q and (run_dir / per_q).exists():
        with st.expander("Per-question scores", expanded=False):
            df = pd.read_csv(run_dir / per_q)
            st.dataframe(df, use_container_width=True)

    labels_csv = artifacts.get("labels_csv")
    if labels_csv and (run_dir / labels_csv).exists():
        with st.expander("Response labels (with reasoning)", expanded=False):
            df = pd.read_csv(run_dir / labels_csv)
            label_filter = st.multiselect(
                "Filter by label",
                options=sorted(df["label"].dropna().unique().tolist()),
                default=None,
                key=f"labels_{summary.get('timestamp')}",
            )
            view = df[df["label"].isin(label_filter)] if label_filter else df
            st.dataframe(view, use_container_width=True)

    cot_csv = artifacts.get("cot_audit_csv")
    if cot_csv and (run_dir / cot_csv).exists():
        with st.expander("CoT audit details", expanded=False):
            df = pd.read_csv(run_dir / cot_csv)
            st.dataframe(df, use_container_width=True)

    logprobs_jsonl = artifacts.get("logprobs_jsonl")
    if logprobs_jsonl and (run_dir / logprobs_jsonl).exists():
        _render_token_logprobs(run_dir / logprobs_jsonl, summary.get("timestamp", ""))


def _render_highlighted_tokens(tokens: list[str], logprobs: list[float]) -> None:
    """Render the answer with low-confidence tokens tinted red.

    Single-hue alpha gradient: high-confidence tokens stay clean (transparent
    background), low-confidence tokens get progressively redder. Keeps most of the
    answer readable as plain text and draws the eye only to spikes. Box is
    height-capped so long answers don't dominate the page."""
    if not tokens:
        return
    lo, hi = min(logprobs), max(logprobs)
    span = hi - lo if hi > lo else 1.0

    def alpha(lp: float) -> float:
        # 0 at highest logprob (transparent), MAX at lowest (most red).
        t = 1.0 - (lp - lo) / span
        return 0.55 * max(0.0, min(1.0, t))

    parts: list[str] = []
    for tok, lp in zip(tokens, logprobs):
        text = html.escape(tok).replace("\n", "<br>")
        bg = f"rgba(220, 70, 70, {alpha(lp):.3f})"
        parts.append(
            f'<span style="background-color:{bg}; padding:1px 2px; '
            f'border-radius:2px;" title="logprob={lp:.4f}">{text}</span>'
        )
    body = "".join(parts)
    st.markdown(
        '<div style="font-family: ui-monospace, monospace; line-height: 1.7; '
        "white-space: pre-wrap; border: 1px solid rgba(127,127,127,0.3); "
        "padding: 10px; border-radius: 4px; max-height: 220px; "
        f'overflow-y: auto;">{body}</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Red intensity ∝ low confidence; clean text = high confidence. "
        f"Range: [{lo:.3f}, {hi:.3f}]. Hover for exact logprob."
    )


@st.cache_data(show_spinner=False)
def _load_token_logprobs(path_str: str, mtime: float) -> list[dict]:
    """Load the per-question token-logprob JSONL. mtime is part of the cache key."""
    rows: list[dict] = []
    with open(path_str, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _render_token_logprobs(path: Path, ts: str) -> None:
    rows = _load_token_logprobs(str(path), path.stat().st_mtime)
    if not rows:
        return
    with st.expander("Per-question token logprobs", expanded=False):
        # Each row label keeps the index so selectbox keys stay unique even when
        # two questions happen to share a leading prefix.
        def _label(i: int, q: str) -> str:
            q = q.replace("\n", " ").strip()
            return f"[{i}] {q[:80]}" + ("…" if len(q) > 80 else "")

        options = {i: _label(i, r.get("question", "")) for i, r in enumerate(rows)}
        selected_i = st.selectbox(
            "Question",
            list(options.keys()),
            format_func=lambda i: options[i],
            key=f"logprobs_q_{ts}",
        )
        row = rows[selected_i]
        tokens = row.get("tokens") or []
        logprobs = row.get("logprobs") or []
        if not tokens:
            st.caption("No tokens captured for this question.")
            return

        df = pd.DataFrame(
            {
                "position": list(range(len(tokens))),
                "token": tokens,
                "logprob": logprobs,
            }
        )

        cols = st.columns(3)
        cols[0].metric("Tokens", len(tokens))
        cols[1].metric("Mean logprob", f"{row.get('mean_logprob', float('nan')):.4f}")
        cols[2].metric("Perplexity", f"{row.get('perplexity', float('nan')):.3f}")

        st.markdown("**Highlighted answer**")
        _render_highlighted_tokens(tokens, logprobs)

        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X("position:Q", title="Token position"),
                y=alt.Y("logprob:Q", title="Logprob (closer to 0 = more confident)"),
                tooltip=[
                    alt.Tooltip("position:Q", title="Pos"),
                    alt.Tooltip("token:N", title="Token"),
                    alt.Tooltip("logprob:Q", title="Logprob", format=".4f"),
                ],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

        n_low = st.slider(
            "Show lowest-N tokens",
            min_value=1,
            max_value=min(50, len(tokens)),
            value=min(10, len(tokens)),
            key=f"logprobs_n_{ts}",
        )
        lowest = df.sort_values("logprob").head(n_low).reset_index(drop=True)
        st.caption(f"Lowest {n_low} tokens (likely confidence dips):")
        st.dataframe(lowest, use_container_width=True)


def _parse_run_timestamp(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
    except (ValueError, TypeError):
        return None


def build_trend_df(runs: list[dict]) -> pd.DataFrame:
    rows = []
    for run in runs:
        summary = run["summary"]
        ts = summary.get("timestamp") or run.get("ts")
        parsed = _parse_run_timestamp(ts)
        row: dict = {"timestamp": ts, "datetime": parsed}
        for _group, specs in TREND_METRIC_GROUPS.items():
            for section, key, label in specs:
                value = summary.get(section, {}).get(key)
                row[label] = float(value) if isinstance(value, (int, float)) else None
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("datetime", na_position="last").reset_index(drop=True)
    labels = df["datetime"].apply(
        lambda d: d.strftime("%m-%d %H:%M") if isinstance(d, datetime) else "?"
    )
    if labels.duplicated().any():
        labels = labels + " #" + (df.groupby(labels).cumcount() + 1).astype(str)
    df = df.drop(columns=["timestamp", "datetime"])
    df.index = pd.Index(labels, name="Run")
    return df


def render_trends(runs: list[dict]) -> None:
    if len(runs) < 2:
        st.caption("Trends require at least two runs.")
        return
    df = build_trend_df(runs)
    if df.empty:
        st.caption("No runs available to plot.")
        return

    for group, specs in TREND_METRIC_GROUPS.items():
        labels = [label for _section, _key, label in specs if label in df.columns]
        group_df = df[labels].dropna(how="all")
        if group_df.empty:
            continue
        st.markdown(f"**{group}**")
        st.line_chart(group_df, use_container_width=True)


def render_compare(a: dict, b: dict) -> None:
    rows = []

    def add(section: str, key: str, label: str):
        rows.append(
            {
                "metric": label,
                a["timestamp"]: a.get(section, {}).get(key),
                b["timestamp"]: b.get(section, {}).get(key),
            }
        )

    for key, label in [
        ("hit_rate", "Hit Rate"),
        ("mrr", "MRR"),
        ("recall", "Recall"),
        ("map", "MAP"),
        ("ndcg_at_5", "nDCG@5"),
        ("ndcg_at_10", "nDCG@10"),
    ]:
        add("retrieval", key, label)
    for key, label in [
        ("rouge1", "ROUGE-1"),
        ("rougeL", "ROUGE-L"),
        ("bleu", "BLEU"),
        ("meteor", "METEOR"),
    ]:
        add("generation", key, label)
    add("judge_scores", "completion_mean", "Completion (judge)")
    add("judge_scores", "precision_mean", "Precision (judge)")
    add("faithfulness", "mean_per_question", "Faithfulness (mean)")
    add("faithfulness", "micro_ratio", "Faithfulness (micro)")
    add("answer_relevancy", "mean_per_question", "Answer relevancy (mean)")
    add("context_relevancy", "mean_per_question", "Context relevancy (mean)")
    add("refusal", "abstention_rate", "Abstention rate")
    add("refusal", "false_refusal_rate", "False-refusal rate")
    add("latency", "mean", "Latency mean")
    add("latency", "p50", "Latency p50")
    add("logprobs", "mean_logprob_mean", "Mean logprob")
    add("logprobs", "perplexity_mean", "Mean perplexity")
    add("calibration", "spearman_completion_perplexity", "Spearman comp↔ppl")
    add("calibration", "spearman_precision_perplexity", "Spearman prec↔ppl")
    add("calibration", "fully_correct_with_low_score", "FC w/ low score (n)")
    add("calibration", "contradictory_with_high_score", "Contra w/ high score (n)")

    df = pd.DataFrame(rows).set_index("metric")
    df["Δ (B − A)"] = df[b["timestamp"]].apply(lambda v: float(v) if v is not None else None) - df[
        a["timestamp"]
    ].apply(lambda v: float(v) if v is not None else None)
    st.dataframe(df, use_container_width=True)


def render_corpus_score() -> None:
    """Render reports/score.csv produced by upload_files.py. Overwritten each upload."""
    if not SCORE_CSV_PATH.exists():
        st.caption(
            f"No corpus score yet. Run `python upload_files.py` to populate "
            f"`{SCORE_CSV_PATH.relative_to(REPO_DIR)}`."
        )
        return

    try:
        df = pd.read_csv(SCORE_CSV_PATH)
    except (OSError, pd.errors.ParserError) as e:
        st.error(f"Could not read {SCORE_CSV_PATH.name}: {e}")
        return

    if df.empty or not {"metric", "value"}.issubset(df.columns):
        st.warning(f"`{SCORE_CSV_PATH.name}` exists but is empty or malformed.")
        return

    mtime = datetime.fromtimestamp(SCORE_CSV_PATH.stat().st_mtime)
    st.caption(f"Last updated: {mtime:%Y-%m-%d %H:%M:%S} · {len(df)} metrics")

    # Job metadata written by upload_files.py so background jobs can be retrieved later.
    meta = dict(zip(df["metric"].astype(str), df["value"].astype(str), strict=False))
    job_lines = []
    for job_type in ("analysis", "audit"):
        jid = meta.get(f"{job_type}_job_id")
        if jid:
            job_lines.append(
                f"**{job_type}** `{jid}` · status: **{meta.get(f'{job_type}_job_status', '?')}**"
            )
    if job_lines:
        st.info(
            "Background jobs (triggered "
            f"{meta.get('retrieved_at', '?')}):\n\n- " + "\n- ".join(job_lines)
        )

    # Coerce numeric where possible; keep the original string for display fallback.
    df = df.copy()
    df["numeric"] = pd.to_numeric(df["value"], errors="coerce")

    # Top-level scalars (no dot in key) are headline metrics — show as tiles.
    top_level = df[~df["metric"].astype(str).str.contains(r"\.", regex=True, na=False)]
    headline = top_level.dropna(subset=["numeric"]).reset_index(drop=True)
    if not headline.empty:
        for chunk_start in range(0, len(headline), 4):
            chunk = headline.iloc[chunk_start : chunk_start + 4]
            cols = st.columns(len(chunk))
            for col, (_idx, row) in zip(cols, chunk.iterrows(), strict=False):
                col.metric(str(row["metric"]), fmt(row["numeric"]))

    # Axis rollups (audit mode) — group rows like axis.<name>.<field>.
    axis_mask = df["metric"].astype(str).str.startswith("axis.")
    axis_rows = df[axis_mask]
    if not axis_rows.empty:
        st.markdown("**Audit axes**")
        parts = axis_rows["metric"].astype(str).str.split(".", n=2, expand=True)
        axis_df = pd.DataFrame(
            {"axis": parts[1], "field": parts[2], "value": axis_rows["value"]}
        )
        pivoted = axis_df.pivot_table(
            index="axis", columns="field", values="value", aggfunc="first"
        )
        st.dataframe(pivoted, use_container_width=True)

    with st.expander("All metrics", expanded=False):
        st.dataframe(df[["metric", "value"]], use_container_width=True)
        st.download_button(
            "Download score.csv",
            data=SCORE_CSV_PATH.read_bytes(),
            file_name="score.csv",
            mime="text/csv",
        )


def render_context_ablation() -> None:
    """Render reports/context_ablation.csv from context_ablation.py.

    Shows the with-context (OpenRAG) answer next to the without-context
    (closed-book, same gen model) answer so you can eyeball how much the
    retrieved chunks actually change the answer. Overwritten each run.
    """
    if not CONTEXT_ABLATION_CSV_PATH.exists():
        st.caption(
            f"No ablation yet. Run `python context_ablation.py` to populate "
            f"`{CONTEXT_ABLATION_CSV_PATH.relative_to(REPO_DIR)}`."
        )
        return

    try:
        df = pd.read_csv(CONTEXT_ABLATION_CSV_PATH)
    except (OSError, pd.errors.ParserError) as e:
        st.error(f"Could not read {CONTEXT_ABLATION_CSV_PATH.name}: {e}")
        return

    expected = {"question", "ground_truth", "with_context", "without_context"}
    if df.empty or not expected.issubset(df.columns):
        st.warning(f"`{CONTEXT_ABLATION_CSV_PATH.name}` exists but is empty or malformed.")
        return

    mtime = datetime.fromtimestamp(CONTEXT_ABLATION_CSV_PATH.stat().st_mtime)
    st.caption(f"Last updated: {mtime:%Y-%m-%d %H:%M:%S} · {len(df)} questions")

    # Per-question side-by-side: pick a question, see all three answers in columns.
    labels = [
        f"Q{i + 1}: {str(q)[:80]}{'…' if len(str(q)) > 80 else ''}"
        for i, q in enumerate(df["question"])
    ]
    choice = st.selectbox("Question", range(len(df)), format_func=lambda i: labels[i])
    row = df.iloc[choice]

    st.markdown(f"**{row['question']}**")
    if "n_retrieved_chunks" in df.columns and pd.notna(row.get("n_retrieved_chunks")):
        st.caption(f"Retrieved {int(row['n_retrieved_chunks'])} chunks")

    cols = st.columns(3)
    cols[0].markdown("**Ground truth**")
    cols[0].info(str(row["ground_truth"]))
    cols[1].markdown("**With context** (OpenRAG)")
    cols[1].success(str(row["with_context"]) if pd.notna(row["with_context"]) else "_(no response)_")
    cols[2].markdown("**Without context** (closed-book)")
    cols[2].warning(str(row["without_context"]) if pd.notna(row["without_context"]) else "_(no response)_")

    with st.expander("All questions (table)", expanded=False):
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "Download context_ablation.csv",
            data=CONTEXT_ABLATION_CSV_PATH.read_bytes(),
            file_name="context_ablation.csv",
            mime="text/csv",
        )


def run_single_query_sync(
    query: str,
    partition: str,
    base_url: str,
    reference_answer: str | None,
    reference_chunk_ids: list[str] | None,
) -> dict:
    """Run benchmark.run_single_query inline (no subprocess, no report files).

    benchmark is imported lazily so the dashboard starts without pulling in the
    judge/LLM dependencies until single-query mode is actually used.
    """
    import asyncio

    import benchmark

    return asyncio.run(
        benchmark.run_single_query(
            query=query,
            partition=partition,
            base_url=base_url,
            reference_answer=reference_answer or None,
            reference_chunk_ids=reference_chunk_ids or None,
        )
    )


def render_single_query_result(result: dict) -> None:
    if result.get("error"):
        st.error(result["error"])
        return

    st.markdown("**Response**")
    st.success(result.get("response") or "_(empty response)_")

    cols = st.columns(4)
    cols[0].metric("Latency (s)", fmt(result.get("latency")))
    cols[1].metric("Perplexity", fmt(result.get("perplexity")))
    cols[2].metric("Mean logprob", fmt(result.get("mean_logprob"), digits=4))
    refusal = result.get("refusal")
    cols[3].metric("Refusal?", "—" if refusal is None else ("Yes" if refusal else "No"))

    def _relevancy_metric(col, label, d):
        if not d:
            return
        col.metric(
            label,
            fmt(d.get("score")),
            help=f"{d.get('relevant')}/{d.get('total')} statements relevant",
        )

    st.markdown("**Reference-free judges**")
    cols = st.columns(3)
    _relevancy_metric(cols[0], "Answer relevancy", result.get("answer_relevancy"))
    _relevancy_metric(cols[1], "Context relevancy", result.get("context_relevancy"))
    _relevancy_metric(cols[2], "Faithfulness", result.get("faithfulness"))

    if "completion" in result or "label" in result:
        st.markdown("**Reference-based judges**")
        cols = st.columns(3)
        cols[0].metric("Completion (1-10)", fmt(result.get("completion")))
        cols[1].metric("Precision (1-10)", fmt(result.get("precision")))
        cols[2].metric("Label", result.get("label", "—"))
        if result.get("label_reasoning"):
            st.caption(result["label_reasoning"])

    gen = result.get("generation")
    if gen:
        st.markdown("**Generation (n-gram overlap)**")
        _render_generation_metrics(gen)

    retr = result.get("retrieval")
    if retr:
        st.markdown("**Retrieval (vs reference chunk IDs)**")
        _render_retrieval_metrics(retr)

    chunk_ids = result.get("chunk_ids") or []
    chunk_texts = result.get("chunk_texts") or []
    chunk_urls = result.get("chunk_urls") or []
    if chunk_ids or chunk_texts:
        with st.expander(f"Retrieved sources ({len(chunk_ids)})", expanded=False):
            for i, cid in enumerate(chunk_ids):
                st.markdown(f"**[Source {i + 1}]** `{cid}`")
                if i < len(chunk_urls) and chunk_urls[i]:
                    st.caption(chunk_urls[i])
                if i < len(chunk_texts) and chunk_texts[i]:
                    st.text(chunk_texts[i][:1500])
                st.divider()


def render_single_query(default_partition: str, default_base_url: str) -> None:
    """Ad-hoc single-query tester.

    Runs one query through OpenRAG and the judges, showing the result inline.
    Nothing is written to ``./reports/`` — this is for quick iteration only.
    """
    st.caption(
        "Type one question, run it against OpenRAG, and see the response, retrieved "
        "sources, and reference-free judge scores. Nothing is saved to `./reports/`."
    )
    with st.form("single_query_form"):
        query = st.text_area("Question", placeholder="Ask a question…", height=80)
        cols = st.columns(2)
        partition = cols[0].text_input("Partition", value=default_partition, key="sq_partition")
        base_url = cols[1].text_input("OpenRAG base URL", value=default_base_url, key="sq_base_url")
        with st.expander(
            "Optional ground truth (enables completion/precision/retrieval metrics)",
            expanded=False,
        ):
            reference_answer = st.text_area("Reference answer", height=80, key="sq_ref_answer")
            ref_chunk_ids_raw = st.text_input(
                "Reference chunk IDs (comma-separated)",
                key="sq_ref_chunks",
                help="If set, ID-based retrieval metrics (hit rate, MRR, …) are computed.",
            )
        submitted = st.form_submit_button("Run single query", type="primary")

    if submitted:
        if not query.strip():
            st.warning("Enter a question first.")
        else:
            ref_chunk_ids = [c.strip() for c in ref_chunk_ids_raw.split(",") if c.strip()]
            with st.spinner("Running query through OpenRAG and judges…"):
                try:
                    st.session_state["single_query_result"] = run_single_query_sync(
                        query.strip(),
                        partition.strip(),
                        base_url.strip(),
                        reference_answer.strip(),
                        ref_chunk_ids,
                    )
                except Exception as e:
                    st.session_state["single_query_result"] = None
                    st.error(f"Single query failed: {e}")

    result = st.session_state.get("single_query_result")
    if result:
        render_single_query_result(result)


def proc_alive() -> bool:
    proc = st.session_state.get("run_proc")
    return proc is not None and proc.poll() is None


def start_run(partition: str, base_url: str, limit: int | None, dataset_path: Path) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{int(time.time())}.log"
    cmd = [
        sys.executable,
        str(BENCHMARK_SCRIPT),
        "--partition",
        partition,
        "--base-url",
        base_url,
        "--dataset",
        str(dataset_path),
        "--output-dir",
        str(REPORTS_DIR),
    ]
    if limit:
        cmd += ["--limit", str(limit)]

    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    st.session_state["run_proc"] = proc
    st.session_state["run_log_path"] = str(log_path)
    st.session_state["run_log_file"] = log_file
    st.session_state["run_started_at"] = time.time()


def stop_run() -> None:
    proc = st.session_state.get("run_proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def gen_alive() -> bool:
    proc = st.session_state.get("gen_proc")
    return proc is not None and proc.poll() is None


def start_generate(
    partition: str,
    n_questions_per_cluster: int,
    n_unanswerable_per_cluster: int,
) -> None:
    """Launch generate_questions.py for a partition. Writes the dataset to ./dataset.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"generate_{int(time.time())}.log"
    cmd = [
        sys.executable,
        str(GENERATE_SCRIPT),
        "--partition",
        partition,
        "--n-questions-per-cluster",
        str(n_questions_per_cluster),
        "--n-unanswerable-per-cluster",
        str(n_unanswerable_per_cluster),
    ]

    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    st.session_state["gen_proc"] = proc
    st.session_state["gen_log_path"] = str(log_path)
    st.session_state["gen_log_file"] = log_file
    st.session_state["gen_started_at"] = time.time()


def stop_generate() -> None:
    proc = st.session_state.get("gen_proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def orch_alive() -> bool:
    proc = st.session_state.get("orch_proc")
    return proc is not None and proc.poll() is None


def save_deploy_input(upload, suffix: str) -> str | None:
    """Persist an uploaded .env / config.yaml to disk so the orchestrator can read it.

    Returns the saved path, or None if nothing was uploaded. Timestamped to avoid
    clobbering a previous run's inputs.
    """
    if upload is None:
        return None
    DEPLOY_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = DEPLOY_INPUTS_DIR / f"{int(time.time())}_{suffix}"
    dest.write_bytes(upload.getvalue())
    return str(dest)


def start_orchestrator(
    version: str,
    versions_repo: str,
    partition: str,
    app_port: int,
    auth_token: str,
    env_overrides: list[str],
    dataset_path: Path,
    limit: int | None,
    prune: bool,
    deploy_env: str | None = None,
    config_path: str | None = None,
    corpus_dir: str | None = None,
    max_files: int | None = None,
    generate_questions: bool = False,
    n_questions_per_cluster: int | None = None,
    n_unanswerable_per_cluster: int | None = None,
    no_retrieval: bool = False,
) -> None:
    """Launch orchestrator.py: deploy <version>, evaluate it, then tear it down.

    The orchestrator writes its report into reports/<uuid>/ and removes the
    instance + per-run data on completion, so the dashboard only needs to watch
    the log and refresh the run list when it finishes (same pattern as benchmark).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"orchestrate_{int(time.time())}.log"
    cmd = [
        sys.executable,
        str(ORCHESTRATOR_SCRIPT),
        "--version", version,
        "--versions-repo", versions_repo,
        "--partition", partition,
        "--dataset", str(dataset_path),
        "--app-port", str(app_port),
        "--auth-token", auth_token,
    ]
    for kv in env_overrides:
        cmd += ["--env", kv]
    if deploy_env:
        cmd += ["--deploy-env", deploy_env]
    if config_path:
        cmd += ["--config", config_path]
    if corpus_dir:
        cmd += ["--corpus-dir", corpus_dir]
    if max_files is not None:
        cmd += ["--max-files", str(max_files)]
    if generate_questions:
        cmd += ["--generate-questions"]
        if n_questions_per_cluster is not None:
            cmd += ["--n-questions-per-cluster", str(n_questions_per_cluster)]
        if n_unanswerable_per_cluster is not None:
            cmd += ["--n-unanswerable-per-cluster", str(n_unanswerable_per_cluster)]
    if no_retrieval:
        cmd += ["--no-retrieval"]
    if limit:
        cmd += ["--limit", str(limit)]
    if not prune:
        cmd += ["--no-prune"]

    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    st.session_state["orch_proc"] = proc
    st.session_state["orch_log_path"] = str(log_path)
    st.session_state["orch_log_file"] = log_file
    st.session_state["orch_started_at"] = time.time()


def stop_orchestrator() -> None:
    """Stop the orchestrator. It tears down the deployed stack in its own ``finally``,
    so terminating it still triggers cleanup unless it is killed outright."""
    proc = st.session_state.get("orch_proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def render_sidebar(runs: list[dict]) -> tuple[dict | None, dict | None, bool]:
    st.sidebar.header("New benchmark run")

    default_partition = CONFIG.common.partition
    default_base_url = CONFIG.common.base_url
    if runs:
        default_partition = runs[0]["summary"].get("config", {}).get("partition", default_partition)
        default_base_url = runs[0]["summary"].get("config", {}).get("base_url", default_base_url)

    partition = st.sidebar.text_input("Partition", value=default_partition)
    base_url = st.sidebar.text_input("OpenRAG base URL", value=default_base_url)
    limit = st.sidebar.number_input("Limit (0 = full dataset)", min_value=0, value=0, step=1)

    st.sidebar.subheader("Dataset")
    source = st.sidebar.radio(
        "Source",
        ["Auto-generated (./dataset.json)", "Upload golden JSON", "Existing path"],
        key="dataset_source",
    )
    dataset_path: Path = DATASET_PATH
    if source == "Upload golden JSON":
        uploaded = st.sidebar.file_uploader("Golden dataset (.json)", type=["json"])
        if uploaded is not None:
            GOLDEN_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            saved = GOLDEN_UPLOAD_DIR / f"golden_{int(time.time())}_{uploaded.name}"
            saved.write_bytes(uploaded.getvalue())
            st.session_state["last_uploaded_golden"] = str(saved)
        last = st.session_state.get("last_uploaded_golden")
        if last:
            dataset_path = Path(last)
            st.sidebar.caption(f"Using upload: `{dataset_path.name}`")
        else:
            st.sidebar.caption("Upload a JSON file to enable the run.")
    elif source == "Existing path":
        path_str = st.sidebar.text_input("Path", value=str(DATASET_PATH), key="dataset_existing_path")
        dataset_path = Path(path_str).expanduser()

    ok, msg, _ = quick_validate_dataset_file(dataset_path)
    if ok:
        st.sidebar.caption(f"✓ {msg}")
    else:
        st.sidebar.error(msg)

    running = proc_alive()
    if not running:
        if st.sidebar.button(
            "Run benchmark", type="primary", use_container_width=True, disabled=not ok
        ):
            start_run(partition, base_url, int(limit) if limit else None, dataset_path)
            st.rerun()
    else:
        st.sidebar.info("Benchmark running…")
        elapsed = int(time.time() - st.session_state.get("run_started_at", time.time()))
        st.sidebar.caption(f"Elapsed: {elapsed}s")
        if st.sidebar.button("Stop", use_container_width=True):
            stop_run()
            st.rerun()
        if st.sidebar.button("Refresh status", use_container_width=True):
            st.rerun()

    log_path = st.session_state.get("run_log_path")
    if log_path and Path(log_path).exists():
        with st.sidebar.expander("Latest run log (tail)", expanded=running):
            try:
                lines = _tail(Path(log_path), 25)
                st.code("\n".join(lines) or "(empty)", language="text")
            except OSError:
                st.caption("(log not readable)")

    st.sidebar.divider()
    st.sidebar.header("Generate dataset")
    st.sidebar.caption(
        f"Cluster the **{partition}** partition's chunks and generate Q&A pairs into "
        f"`{DATASET_PATH.name}` (overwrites it). Then run a benchmark with the "
        "'Auto-generated' dataset source above."
    )
    gen_cols = st.sidebar.columns(2)
    n_questions_per_cluster = gen_cols[0].number_input(
        "Answerable / cluster",
        min_value=0,
        value=int(CONFIG.question_gen.n_questions_per_cluster),
        step=1,
        key="gen_n_questions",
    )
    n_unanswerable_per_cluster = gen_cols[1].number_input(
        "Unanswerable / cluster",
        min_value=0,
        value=int(CONFIG.question_gen.n_unanswerable_per_cluster),
        step=1,
        key="gen_n_unanswerable",
    )

    generating = gen_alive()
    if not generating:
        if st.sidebar.button(
            "Generate dataset", use_container_width=True, disabled=running
        ):
            start_generate(
                partition,
                int(n_questions_per_cluster),
                int(n_unanswerable_per_cluster),
            )
            st.rerun()
        if running:
            st.sidebar.caption("Wait for the current benchmark to finish first.")
    else:
        st.sidebar.info("Generating dataset…")
        elapsed = int(time.time() - st.session_state.get("gen_started_at", time.time()))
        st.sidebar.caption(f"Elapsed: {elapsed}s")
        if st.sidebar.button("Stop generation", use_container_width=True):
            stop_generate()
            st.rerun()
        if st.sidebar.button("Refresh generation status", use_container_width=True):
            st.rerun()

    gen_log_path = st.session_state.get("gen_log_path")
    if gen_log_path and Path(gen_log_path).exists():
        with st.sidebar.expander("Latest generation log (tail)", expanded=generating):
            try:
                lines = _tail(Path(gen_log_path), 25)
                st.code("\n".join(lines) or "(empty)", language="text")
            except OSError:
                st.caption("(log not readable)")

    st.sidebar.divider()
    st.sidebar.header("Deploy & evaluate a version")
    st.sidebar.caption(
        "Check out a git version in a separate OpenRAG clone, deploy it locally with "
        "Docker, ingest the corpus, run the benchmark, then tear the instance down and "
        "delete its data. Each run is saved under `reports/<uuid>/` and is comparable "
        "above. Runs are sequential — finish the current job first."
    )
    deploy_version = st.sidebar.text_input(
        "Git version (tag / branch / commit)", value="", placeholder="v1.2.0", key="deploy_version"
    )
    versions_repo = st.sidebar.text_input(
        "Versions repo path",
        value=st.session_state.get("deploy_versions_repo", DEFAULT_VERSIONS_REPO),
        key="deploy_versions_repo",
        help="A second OpenRAG clone used only for checkout + deploy (kept separate from this eval repo).",
    )
    dcols = st.sidebar.columns(2)
    app_port = dcols[0].number_input("App port", min_value=1, value=8080, step=1, key="deploy_port")
    deploy_auth_token = dcols[1].text_input(
        "AUTH_TOKEN", value=os.environ.get("AUTH_TOKEN", ""), type="password", key="deploy_auth"
    )
    corpus_dir = st.sidebar.text_input(
        "Corpus dir (optional)",
        value="",
        key="deploy_corpus_dir",
        placeholder=str(Path(CONFIG.upload.dir_path)),
        help=(
            "Documents to ingest into this run. Leave blank to use the configured "
            f"corpus (`{CONFIG.upload.dir_path}`). Point at a small subset to avoid "
            "ingesting a large corpus on every run."
        ),
    )
    max_files = st.sidebar.number_input(
        "Max files to ingest (0 = all)",
        min_value=0,
        value=0,
        step=1,
        key="deploy_max_files",
        help="Cap how many files from the corpus get ingested. 0 ingests all available files.",
    )
    gen_questions = st.sidebar.checkbox(
        "Generate questions from ingested corpus (self-contained)",
        value=False,
        key="deploy_gen_questions",
        help=(
            "After ingest, build the dataset from THIS run's indexed chunks so chunk IDs "
            "match → retrieval metrics are non-zero. Saved to reports/<uuid>/dataset.json. "
            "Note: each run gets its own questions, so runs aren't directly comparable."
        ),
    )
    gen_q = gen_unans = None
    if gen_questions:
        gcols = st.sidebar.columns(2)
        gen_q = gcols[0].number_input(
            "Answerable / cluster",
            min_value=0,
            value=int(CONFIG.question_gen.n_questions_per_cluster),
            step=1,
            key="deploy_gen_q",
        )
        gen_unans = gcols[1].number_input(
            "Unanswerable / cluster",
            min_value=0,
            value=int(CONFIG.question_gen.n_unanswerable_per_cluster),
            step=1,
            key="deploy_gen_unans",
        )
    no_retrieval = st.sidebar.checkbox(
        "Skip retrieval metrics (generation + judges only)",
        value=False,
        key="deploy_no_retrieval",
        help=(
            "Don't score ID-based retrieval (hit rate, MRR, nDCG, …). Generation metrics and "
            "all judges still run. Use when the dataset's chunk IDs don't match this run's "
            "index (e.g. a fixed dataset against a freshly re-indexed instance), so retrieval "
            "would be misleading zeros."
        ),
    )
    env_overrides_raw = st.sidebar.text_area(
        "Config overrides (one KEY=VALUE per line)",
        value="",
        height=80,
        key="deploy_env",
        help="Applied to the deployed instance and recorded in run_config.json (e.g. CHUNKER=semantic_splitter).",
    )
    env_file_upload = st.sidebar.file_uploader(
        "Instance .env file (optional)",
        type=None,
        key="deploy_env_file",
        help="Overrides the versions repo's .env. AUTH_TOKEN is read from here if present.",
    )
    config_upload = st.sidebar.file_uploader(
        "Instance config.yaml (optional)",
        type=["yaml", "yml"],
        key="deploy_config_file",
        help="Hydra config deployed with the instance (copied into the build context).",
    )
    prune_on_teardown = st.sidebar.checkbox(
        "Prune dangling images + build cache on teardown",
        value=True,
        key="deploy_prune",
        help="Reclaims orphaned openrag image layers and build cache. Leaves base images and model caches.",
    )

    env_overrides = [ln.strip() for ln in env_overrides_raw.splitlines() if ln.strip()]
    bad_env = [ln for ln in env_overrides if "=" not in ln]

    orchestrating = orch_alive()
    deploy_blocked = running or generating or orchestrating
    # A token may instead come from the uploaded .env (orchestrator reads AUTH_TOKEN from it).
    has_token = bool(deploy_auth_token.strip()) or env_file_upload is not None
    can_deploy = (
        bool(deploy_version.strip())
        and bool(versions_repo.strip())
        and has_token
        and not bad_env
        and ok
    )
    if bad_env:
        st.sidebar.error(f"Override(s) missing '=': {', '.join(bad_env)}")

    if not orchestrating:
        if st.sidebar.button(
            "Deploy & evaluate", type="primary", use_container_width=True, disabled=deploy_blocked or not can_deploy
        ):
            start_orchestrator(
                version=deploy_version.strip(),
                versions_repo=versions_repo.strip(),
                partition=partition,
                app_port=int(app_port),
                auth_token=deploy_auth_token.strip(),
                env_overrides=env_overrides,
                dataset_path=dataset_path,
                limit=int(limit) if limit else None,
                prune=prune_on_teardown,
                deploy_env=save_deploy_input(env_file_upload, "instance.env"),
                config_path=save_deploy_input(config_upload, "config.yaml"),
                corpus_dir=corpus_dir.strip() or None,
                max_files=int(max_files) if max_files else None,
                generate_questions=gen_questions,
                n_questions_per_cluster=int(gen_q) if gen_q is not None else None,
                n_unanswerable_per_cluster=int(gen_unans) if gen_unans is not None else None,
                no_retrieval=no_retrieval,
            )
            st.rerun()
        if deploy_blocked:
            st.sidebar.caption("Another job is running — versions deploy sequentially.")
    else:
        st.sidebar.info("Deploying & evaluating a version…")
        elapsed = int(time.time() - st.session_state.get("orch_started_at", time.time()))
        st.sidebar.caption(f"Elapsed: {elapsed}s")
        if st.sidebar.button("Stop (tears down instance)", use_container_width=True):
            stop_orchestrator()
            st.rerun()
        if st.sidebar.button("Refresh deploy status", use_container_width=True):
            st.rerun()

    orch_log_path = st.session_state.get("orch_log_path")
    if orch_log_path and Path(orch_log_path).exists():
        with st.sidebar.expander("Latest deploy log (tail)", expanded=orchestrating):
            try:
                lines = _tail(Path(orch_log_path), 30)
                st.code("\n".join(lines) or "(empty)", language="text")
            except OSError:
                st.caption("(log not readable)")

    st.sidebar.divider()
    st.sidebar.header("Runs")
    if not runs:
        st.sidebar.caption("No runs found. Trigger one above.")
        return None, None, running

    options = {run_label(r): r for r in runs}
    selected_key = st.sidebar.selectbox("View run", list(options.keys()), index=0)
    selected = options[selected_key]

    compare = st.sidebar.toggle("Compare against another run", value=False)
    compare_run = None
    if compare and len(options) > 1:
        other_keys = [k for k in options if k != selected_key]
        compare_key = st.sidebar.selectbox("Compare to", other_keys, index=0)
        compare_run = options[compare_key]

    return selected, compare_run, running


def main() -> None:
    st.set_page_config(page_title="RAG Eval Dashboard", layout="wide")
    st.title("RAG Evaluation Dashboard")

    runs = list_runs()
    selected, compare_run, running = render_sidebar(runs)

    if running:
        proc = st.session_state["run_proc"]
        if proc.poll() is not None:
            st.session_state.pop("run_proc", None)
            log_file = st.session_state.pop("run_log_file", None)
            if log_file:
                log_file.close()
            st.toast("Benchmark finished — refreshing run list", icon="✅")
            time.sleep(0.5)
            st.rerun()
        else:
            st.info("A benchmark is running in the background. Use the sidebar to monitor progress.")

    gen_proc = st.session_state.get("gen_proc")
    if gen_proc is not None:
        if gen_proc.poll() is not None:
            returncode = gen_proc.returncode
            st.session_state.pop("gen_proc", None)
            log_file = st.session_state.pop("gen_log_file", None)
            if log_file:
                log_file.close()
            if returncode == 0:
                st.toast(f"Dataset generated — wrote {DATASET_PATH.name}", icon="✅")
            else:
                st.toast(f"Dataset generation failed (exit {returncode}) — see log", icon="⚠️")
            time.sleep(0.5)
            st.rerun()
        else:
            st.info("Dataset generation is running in the background. Use the sidebar to monitor progress.")

    orch_proc = st.session_state.get("orch_proc")
    if orch_proc is not None:
        if orch_proc.poll() is not None:
            returncode = orch_proc.returncode
            st.session_state.pop("orch_proc", None)
            log_file = st.session_state.pop("orch_log_file", None)
            if log_file:
                log_file.close()
            if returncode == 0:
                st.toast("Version deployed, evaluated, and torn down — refreshing runs", icon="✅")
            else:
                st.toast(f"Version run failed (exit {returncode}) — instance torn down, see log", icon="⚠️")
            time.sleep(0.5)
            st.rerun()
        else:
            st.info(
                "Deploying & evaluating a version in the background (build → ingest → "
                "benchmark → teardown). Use the sidebar to monitor progress."
            )

    default_partition = CONFIG.common.partition
    default_base_url = CONFIG.common.base_url
    if runs:
        default_partition = runs[0]["summary"].get("config", {}).get("partition", default_partition)
        default_base_url = runs[0]["summary"].get("config", {}).get("base_url", default_base_url)

    with st.expander("🔍 Single query (ad-hoc test — not saved)", expanded=False):
        render_single_query(default_partition, default_base_url)

    with st.expander("Corpus SCORE (latest upload)", expanded=False):
        render_corpus_score()

    with st.expander("Context ablation (with vs without context)", expanded=False):
        render_context_ablation()

    if selected is None:
        st.write("No evaluation runs found in `./reports/`. Trigger one from the sidebar.")
        return

    summary = selected["summary"]

    # The run immediately older than the selected one (runs is newest-first),
    # used to show "vs previous run" trend arrows on the scorecard.
    prev_summary = None
    for i, r in enumerate(runs):
        if r is selected:
            if i + 1 < len(runs):
                prev_summary = runs[i + 1]["summary"]
            break

    with st.expander(f"Trends across {len(runs)} runs", expanded=False):
        render_trends(runs)

    if compare_run is None:
        st.subheader(f"Run: {selected['ts']}")
        render_scorecard(summary, prev_summary)
        render_summary(summary)
        render_artifacts(summary, selected["dir"])
    else:
        st.subheader(f"Compare: {selected['ts']}  ↔  {compare_run['ts']}")
        render_compare(selected["summary"], compare_run["summary"])
        with st.expander(f"Full details — {selected['ts']}", expanded=False):
            render_summary(selected["summary"])
            render_artifacts(selected["summary"], selected["dir"])
        with st.expander(f"Full details — {compare_run['ts']}", expanded=False):
            render_summary(compare_run["summary"])
            render_artifacts(compare_run["summary"], compare_run["dir"])


if __name__ == "__main__":
    main()
