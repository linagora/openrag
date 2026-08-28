import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import hdbscan
import httpx
import numpy as np
import umap.umap_ as umap
import utils
from config import CONFIG
from dotenv import load_dotenv
from evaluation_prompts import (
    ANSWER_TMPL_EN,
    ANSWERABLE_CRITIC_PROMPT_EN,
    CAPABILITY_QUESTION_INSTRUCTIONS_EN,
    GENERIC_QUESTION_INSTRUCTION_EN,
    QUESTION_TMPL_EN,
    QUESTION_TYPE_INSTRUCTIONS_EN,
    REFUSAL_REFERENCE_EN,
    UNANSWERABLE_CRITIC_PROMPT_EN,
    UNANSWERABLE_QUESTION_TMPL_EN,
)
from langchain_openai import ChatOpenAI
from loguru import logger
from sklearn.cluster import DBSCAN, KMeans
from tqdm.asyncio import tqdm

load_dotenv()

BASE_URL = os.environ["BASE_URL"]
API_KEY = os.environ["API_KEY"]
MODEL = os.environ["MODEL"]
OPENRAG_AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

# Filtration-critic endpoint. Defaults to the generation model/endpoint, but can be
# pointed at a different (ideally stronger) model: a model critiquing its OWN output
# suffers self-preference bias and shares its own blind spots, which most undermines
# the `faithful` and `unanswerable` verdicts. Mirrors the JUDGE_MODEL /
# JUDGE_BASE_URL / JUDGE_API_KEY convention in benchmark.py.
CRITIC_MODEL = os.environ.get("CRITIC_MODEL") or MODEL
CRITIC_BASE_URL = os.environ.get("CRITIC_BASE_URL") or BASE_URL
CRITIC_API_KEY = os.environ.get("CRITIC_API_KEY") or API_KEY

settings = {
    "temperature": CONFIG.question_gen.temperature,
    "max_retries": CONFIG.question_gen.max_retries,
    "timeout": CONFIG.question_gen.timeout,
    "base_url": BASE_URL,
    "model": MODEL,
    "api_key": API_KEY,
    "max_tokens": CONFIG.question_gen.max_tokens,
}

llm = ChatOpenAI(**settings).with_retry(stop_after_attempt=CONFIG.question_gen.stop_after_attempt)

# Critic LLM for the filtration pass. Low temperature for deterministic scoring, and
# its own model/endpoint (defaulting to the generator's — see CRITIC_MODEL above).
# The critic is prompted to emit strict JSON and parsed defensively (no native
# structured-output / function-calling dependency), so it works against any
# OpenAI-compatible proxy.
critic_settings = {
    **settings,
    "model": CRITIC_MODEL,
    "base_url": CRITIC_BASE_URL,
    "api_key": CRITIC_API_KEY,
    "temperature": CONFIG.question_gen.filtration.critic_temperature,
}
critic_llm = ChatOpenAI(**critic_settings).with_retry(
    stop_after_attempt=CONFIG.question_gen.stop_after_attempt
)

if CONFIG.question_gen.filtration.enabled:
    logger.info(f"Filtration critic: {CRITIC_MODEL} @ {CRITIC_BASE_URL}")
    if CRITIC_MODEL == MODEL and CRITIC_BASE_URL == BASE_URL:
        # Not fatal — the critic still catches plenty — but self-judging inflates
        # `faithful`/`unanswerable` verdicts, so make the caveat visible in the logs.
        logger.warning(
            "Filtration critic is the SAME model as the generator — it is judging its own "
            "output (self-preference bias / shared blind spots). Set CRITIC_MODEL "
            "(and optionally CRITIC_BASE_URL / CRITIC_API_KEY) to use a different model."
        )

# Per-type generation spec: difficulty label, minimum chunks required, and whether
# the type is inherently multi-hop. Keys MUST match
# QUESTION_TYPE_INSTRUCTIONS_EN / config.question_gen.typing.distribution.
QUESTION_TYPE_SPEC = {
    # --- distribution profile (default) ---
    "single_hop_specific": {"difficulty": "easy", "min_chunks": 1, "multi_hop": False},
    "multi_hop_specific": {"difficulty": "hard", "min_chunks": 2, "multi_hop": True},
    "reasoning": {"difficulty": "medium", "min_chunks": 1, "multi_hop": False},
    "comparative": {"difficulty": "medium", "min_chunks": 2, "multi_hop": True},
    # --- capability-suite profile (also registered here so generate_qa can label
    # them; chunk selection lives in the capability driver, not the distribution one,
    # so these are inert for the distribution profile) ---
    "table_lookup": {"difficulty": "medium", "min_chunks": 1, "multi_hop": False},
    "multi_hop_retrieval": {"difficulty": "hard", "min_chunks": 2, "multi_hop": True},
    "cross_document_reasoning": {"difficulty": "hard", "min_chunks": 2, "multi_hop": True},
    "citation_grounding": {"difficulty": "medium", "min_chunks": 1, "multi_hop": False},
    "long_context_retrieval": {"difficulty": "hard", "min_chunks": 3, "multi_hop": True},
    "numerical_reasoning": {"difficulty": "medium", "min_chunks": 1, "multi_hop": False},
}
DEFAULT_QUESTION_TYPE = "single_hop_specific"

# Merged instruction lookup used by generate_qa for BOTH profiles. Unknown keys
# (e.g. "generic") fall back to GENERIC_QUESTION_INSTRUCTION_EN at the call site.
QUESTION_TYPE_INSTRUCTIONS = {
    **QUESTION_TYPE_INSTRUCTIONS_EN,
    **CAPABILITY_QUESTION_INSTRUCTIONS_EN,
}

# --- Deterministic self-containment gate -------------------------------------
# Whether a question points at its own source container is a MECHANICAL property,
# and the LLM critic proved unreliable at it: on a real run it scored questions
# reading "...the distribution in Document 2..." a perfect self_containment of 1.0
# (40% of answerable questions leaked through). So decide this with a regex and let
# it override the critic, rather than trusting the model to catch its own habit.
CONTAINER_REFERENCE_PATTERN = re.compile(
    r"\bdocuments?\s+\d+"
    r"|\bthe\s+(?:provided\s+|given\s+|above\s+|following\s+)?documents?\b"
    r"|\bthe\s+(?:provided\s+|given\s+|above\s+)?(?:context|passage|excerpt|snippet)s?\b"
    r"|\bthe\s+text\s+(?:above|below|provided|given)\b"
    r"|\baccording\s+to\s+the\s+(?:document|text|passage|context|excerpt)"
    r"|\bin\s+the\s+(?:research\s+)?(?:paper|article|study)\b"
    r"|\bin\s+the\s+(?:references?|acknowledge?ments?|bibliography)\b"
    r"|\b(?:mentioned|shown|described|listed|stated)\s+in\s+the\s+(?:references?|acknowledge?ments?|bibliography)\b"
    r"|\b(?:mentioned|shown|described|listed)\s+above\b",
    re.IGNORECASE,
)


def _references_container(question: str) -> bool:
    """True if the question refers to its source container instead of standing alone.

    Such a question can't meaningfully be asked of a RAG system: there is no
    "Document 2" at query time.
    """
    return bool(CONTAINER_REFERENCE_PATTERN.search(question))


# --- Chunk-quality gate (runs before embedding/clustering) --------------------
BOILERPLATE_HEADING_PATTERN = re.compile(
    r"^#{0,6}\s*\**\s*(?:references?|bibliography|acknowledge?ments?|conflicts? of interests?"
    r"|funding|author contributions?|declarations?|data availability|table of contents)\b",
    re.IGNORECASE | re.MULTILINE,
)
# Bibliography ENTRY lines ("[1] A. Cayley, On the Conic..."), not bare "[n]" —
# in a maths corpus "[1]" also appears inside formulae, and counting those would
# drop legitimate content.
CITATION_LINE_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:<span[^>]*>\s*</span>\s*)?\[\d+\]\s+[A-Z]", re.MULTILINE
)
COPYRIGHT_PATTERN = re.compile(
    r"copyright\s*©|all rights reserved|licensed under|creative commons", re.IGNORECASE
)


def _chunk_quality_reason(text: str) -> str | None:
    """Why this chunk is unfit for question generation, or None if it's usable."""
    cfg = CONFIG.question_gen.chunk_filter
    body = utils.chunk_body(text)
    if len(body) < cfg.min_chars:
        return "too_short"
    if COPYRIGHT_PATTERN.search(body):
        return "copyright_boilerplate"
    if len(CITATION_LINE_PATTERN.findall(body)) >= cfg.min_citation_lines:
        return "reference_list"
    match = BOILERPLATE_HEADING_PATTERN.search(body)
    if match and (len(body) - match.start()) / len(body) >= cfg.boilerplate_fraction:
        return "boilerplate_section"
    return None


@dataclass
class GenCtx:
    """Run-scoped generation context threaded into every generator task."""

    partition: str
    clustering_method: str
    run_id: str
    language: str
    answer_instruction: str
    unanswerable_instruction: str


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _usage(message) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from a LangChain AIMessage."""
    um = getattr(message, "usage_metadata", None) or {}
    return int(um.get("input_tokens", 0) or 0), int(um.get("output_tokens", 0) or 0)


def _parse_critic_json(raw: str) -> dict | None:
    """Parse a critic response into a dict, tolerating fences / stray prose.

    Thin wrapper over ``utils.loose_json`` (shared with benchmark.py's judge
    parsing) that narrows the result to a dict. Returns None when nothing
    parseable is found (caller treats that as 'cannot verify').
    """
    if not raw:
        return None
    obj = utils.loose_json(raw)
    return obj if isinstance(obj, dict) else None


def _answerable_passed(verdict: dict | None) -> bool:
    """True if an answerable item clears the critic thresholds.

    A missing/unparseable verdict (None) passes: we never block an item we could
    not verify, but the reason is recorded in metadata so it stays auditable.
    """
    if not verdict:
        return True
    f = CONFIG.question_gen.filtration
    try:
        return (
            float(verdict.get("answerability", 0)) >= f.answerability_threshold
            and float(verdict.get("self_containment", 0)) >= f.self_containment_threshold
            and float(verdict.get("clarity", 0)) >= f.clarity_threshold
            and (bool(verdict.get("faithful", True)) or not f.require_faithful)
        )
    except (TypeError, ValueError):
        return True


def _unanswerable_passed(verdict: dict | None) -> bool:
    if not verdict:
        return True
    f = CONFIG.question_gen.filtration
    try:
        return (
            bool(verdict.get("unanswerable", False))
            and float(verdict.get("self_containment", 0)) >= f.self_containment_threshold
            and float(verdict.get("clarity", 0)) >= f.clarity_threshold
        )
    except (TypeError, ValueError):
        return True


def _reasons(verdict: dict | None, passed: bool, container_ref: bool = False) -> list[str]:
    if passed:
        return []
    reasons: list[str] = []
    if container_ref:
        # Deterministic gate; recorded separately because the critic often rates
        # these self_contained=1.0 and would otherwise leave no trace of the reject.
        reasons.append("references_source_container")
    if verdict is None:
        reasons.append("critic_unparseable")
    else:
        raw = verdict.get("reasons") or []
        reasons.extend([str(r) for r in raw] if isinstance(raw, list) else [str(raw)])
    return reasons


def _build_source(chunks: list[dict], cluster_id, ctx: GenCtx) -> dict:
    return {
        "partition": ctx.partition,
        "cluster_id": cluster_id,
        "clustering_method": ctx.clustering_method,
        "source_file_ids": sorted({c["file_id"] for c in chunks}),
        # Human-readable filenames alongside the opaque ids; document_category is
        # filled in by _annotate_document_categories() once generation completes.
        "source_filenames": sorted({utils.filename_of(c) for c in chunks if utils.filename_of(c)}),
        "document_category": None,
        "source_chunk_ids": [c["id"] for c in chunks],
        "n_chunks_sampled": len(chunks),
    }


def _build_generation(qid: str, ctx: GenCtx, prompt_tok: int, comp_tok: int, t0: float) -> dict:
    return {
        "generator_model": MODEL,
        "prompt_version": CONFIG.question_gen.prompt_version,
        "temperature": CONFIG.question_gen.temperature,
        "seed": CONFIG.question_gen.seed,
        "created_at": _utc_now_iso(),
        "run_id": ctx.run_id,
        # Deterministic per-item correlation handle within a run; link to an OTel
        # / Langfuse span if a tracer is wired into the generator later.
        "trace_id": f"{ctx.run_id}:{qid}",
        "token_usage": {"prompt": prompt_tok, "completion": comp_tok},
        "latency_ms": round((time.perf_counter() - t0) * 1000),
    }


async def _classifier_ainvoke(prompt: str) -> str:
    """Async (prompt -> text) adapter over the critic LLM, for utils.resolve_*.

    Uses the critic (CRITIC_MODEL) rather than the generator: classification is a
    judgement task, and the critic may point at a stronger model.
    """
    out = await critic_llm.ainvoke([{"role": "user", "content": prompt}])
    return out.content


def format_chunks(chunks: list[str]) -> str:
    chunks_str = ""
    for i, chunk in enumerate(chunks, start=1):
        chunks_str += f"Document {i}:\n{chunk}\n"
        chunks_str += "-" * 40 + "\n"
    return chunks_str.strip()


async def _critic_answerable(chunks_str: str, question: str, answer: str) -> tuple[dict | None, int, int]:
    messages = [
        {"role": "system", "content": ANSWERABLE_CRITIC_PROMPT_EN},
        {
            "role": "user",
            "content": f"DOCUMENTS:\n{chunks_str}\n\nQUESTION: {question}\n\nANSWER: {answer}",
        },
    ]
    try:
        output = await critic_llm.ainvoke(messages)
    except Exception as e:  # critic failure must never abort generation
        logger.debug(f"Answerable critic call failed: {e}")
        return None, 0, 0
    p, c = _usage(output)
    return _parse_critic_json(output.content), p, c


async def _critic_unanswerable(chunks_str: str, question: str) -> tuple[dict | None, int, int]:
    messages = [
        {"role": "system", "content": UNANSWERABLE_CRITIC_PROMPT_EN},
        {"role": "user", "content": f"DOCUMENTS:\n{chunks_str}\n\nQUESTION: {question}"},
    ]
    try:
        output = await critic_llm.ainvoke(messages)
    except Exception as e:
        logger.debug(f"Unanswerable critic call failed: {e}")
        return None, 0, 0
    p, c = _usage(output)
    return _parse_critic_json(output.content), p, c


async def generate_qa(
    chunks: list[dict],
    semaphore: asyncio.Semaphore,
    *,
    qid: str,
    cluster_id,
    qtype: str,
    ctx: GenCtx,
):
    """Generate an answerable (question, answer) pair from the chunks.

    The question is generated with a type-specific instruction (``qtype``); when
    filtration is enabled, an LLM critic scores the item and it is regenerated up
    to ``filtration.max_quality_retries`` times until it clears the thresholds.
    Rich metadata (id, source provenance, generation tracing, quality verdict,
    typed diversity) is attached under ``metadata``.
    """
    # qtype is resolved by the CALLER (distribution driver honours typing.enabled and
    # passes "generic" when off; capability driver passes the capability name). This
    # keeps generate_qa a pure worker shared by both profiles.
    spec = QUESTION_TYPE_SPEC.get(qtype, QUESTION_TYPE_SPEC[DEFAULT_QUESTION_TYPE])
    type_instruction = QUESTION_TYPE_INSTRUCTIONS.get(qtype, GENERIC_QUESTION_INSTRUCTION_EN)
    filt = CONFIG.question_gen.filtration
    max_attempts = (filt.max_quality_retries + 1) if filt.enabled else 1

    async with semaphore:
        t0 = time.perf_counter()
        prompt_tok = comp_tok = 0
        chunks_str = format_chunks([c["text"] for c in chunks])

        llm_question = llm_answer = ""
        verdict: dict | None = None
        container_ref = False
        attempt = 0
        for attempt in range(max_attempts):
            # 1) generate a question based on the chunks
            messages = [
                {"role": "system", "content": QUESTION_TMPL_EN},
                {"role": "user", "content": f"Here are the documents:\n{chunks_str}. {type_instruction}"},
            ]
            output = await llm.ainvoke(messages)
            llm_question = output.content.strip()
            p, c = _usage(output)
            prompt_tok += p
            comp_tok += c

            # 2) generate an answer based on the question and chunks
            messages = [
                {"role": "system", "content": ANSWER_TMPL_EN},
                {
                    "role": "user",
                    "content": f"Here are the documents:\n{chunks_str}\n\nQuestion: {llm_question}.\n{ctx.answer_instruction}",
                },
            ]
            output = await llm.ainvoke(messages)
            llm_answer = output.content.strip()
            p, c = _usage(output)
            prompt_tok += p
            comp_tok += c

            # 3) filtration: deterministic self-containment gate + LLM critic.
            # The regex gate is authoritative for container references — the critic
            # rubber-stamps them (it scored "...in Document 2" a perfect 1.0).
            if not filt.enabled:
                break
            container_ref = _references_container(llm_question)
            verdict, p, c = await _critic_answerable(chunks_str, llm_question, llm_answer)
            prompt_tok += p
            comp_tok += c
            if not container_ref and _answerable_passed(verdict):
                break

        passed = (not container_ref and _answerable_passed(verdict)) if filt.enabled else True
        n_hops = len(chunks) if spec["multi_hop"] else 1
        return {
            "id": qid,
            "question": llm_question,
            "chunks": chunks,
            "llm_answer": llm_answer,
            "answerable": True,
            "metadata": {
                "schema_version": CONFIG.question_gen.schema_version,
                "question_type": qtype,
                "difficulty": spec["difficulty"],
                "n_hops": n_hops,
                "evolution": ["base", qtype],
                "language": ctx.language,
                "persona": None,
                "source": _build_source(chunks, cluster_id, ctx),
                "quality": {
                    "answerability": (verdict or {}).get("answerability"),
                    # The critic's (unreliable) score is kept for transparency; the
                    # deterministic verdict below is what actually gates the item.
                    "self_containment": (verdict or {}).get("self_containment"),
                    "references_container": container_ref,
                    "clarity": (verdict or {}).get("clarity"),
                    "faithfulness_verified": (verdict or {}).get("faithful"),
                    "unanswerable_verified": None,
                    "passed": passed,
                    "regen_attempts": attempt,
                    "reject_reasons": _reasons(verdict, passed, container_ref),
                },
                "generation": _build_generation(qid, ctx, prompt_tok, comp_tok, t0),
            },
        }


async def generate_unanswerable(
    chunks: list[dict],
    semaphore: asyncio.Semaphore,
    *,
    qid: str,
    cluster_id,
    ctx: GenCtx,
):
    """Generate a topic-adjacent question whose answer is NOT in the provided chunks.

    When filtration is enabled, a critic verifies the answer is genuinely absent
    from the chunks (rejecting items that turn out to be answerable) and the
    question is regenerated until it clears the threshold.
    """
    filt = CONFIG.question_gen.filtration
    max_attempts = (filt.max_quality_retries + 1) if filt.enabled else 1

    async with semaphore:
        t0 = time.perf_counter()
        prompt_tok = comp_tok = 0
        chunks_str = format_chunks([c["text"] for c in chunks])

        llm_question = ""
        verdict: dict | None = None
        container_ref = False
        attempt = 0
        for attempt in range(max_attempts):
            messages = [
                {"role": "system", "content": UNANSWERABLE_QUESTION_TMPL_EN},
                {"role": "user", "content": f"Here is the context:\n{chunks_str}\n\n{ctx.unanswerable_instruction}"},
            ]
            output = await llm.ainvoke(messages)
            llm_question = output.content.strip()
            p, c = _usage(output)
            prompt_tok += p
            comp_tok += c

            if not filt.enabled:
                break
            # Deterministic self-containment gate (see generate_qa) + LLM critic.
            container_ref = _references_container(llm_question)
            verdict, p, c = await _critic_unanswerable(chunks_str, llm_question)
            prompt_tok += p
            comp_tok += c
            if not container_ref and _unanswerable_passed(verdict):
                break

        passed = (not container_ref and _unanswerable_passed(verdict)) if filt.enabled else True
        return {
            "id": qid,
            "question": llm_question,
            "chunks": [],
            "llm_answer": REFUSAL_REFERENCE_EN,
            "answerable": False,
            "topic_chunks": [c["id"] for c in chunks],
            "metadata": {
                "schema_version": CONFIG.question_gen.schema_version,
                "question_type": "unanswerable",
                "difficulty": "hard",
                "n_hops": 0,
                "evolution": ["base", "unanswerable"],
                "language": ctx.language,
                "persona": None,
                "source": _build_source(chunks, cluster_id, ctx),
                "quality": {
                    "answerability": None,
                    "self_containment": (verdict or {}).get("self_containment"),
                    "references_container": container_ref,
                    "clarity": (verdict or {}).get("clarity"),
                    "faithfulness_verified": None,
                    # NOTE: verifies the answer is absent from the *sampled* topic
                    # chunks only. A topic-adjacent question answerable from a
                    # different cluster's chunks is not caught here — corpus-wide
                    # verification would need the question embedded against all
                    # chunks (follow-up; needs the embedder endpoint).
                    "unanswerable_verified": (verdict or {}).get("unanswerable"),
                    "passed": passed,
                    "regen_attempts": attempt,
                    "reject_reasons": _reasons(verdict, passed, container_ref),
                },
                "generation": _build_generation(qid, ctx, prompt_tok, comp_tok, t0),
            },
        }


class ChunkFetchError(RuntimeError):
    """Raised when a partition's chunks can't be fetched, carrying an actionable
    cause (auth failure, partition not found, unreachable server, or empty
    partition). Subclasses RuntimeError so any existing broad catch still works;
    __main__ catches it specifically to exit cleanly instead of dumping a traceback.
    """


async def get_all_chunks(url: str) -> list:
    """Fetch a partition's chunks, or raise ChunkFetchError with a clear cause.

    Definitive failures (401/403 auth, 404 partition-not-found, empty partition)
    fail fast — retrying can't fix them. Transient failures (connection errors,
    timeouts, 5xx) are retried up to ``fetch_retries`` before giving up with the
    last observed cause.
    """
    retries = CONFIG.question_gen.fetch_retries
    headers = {}
    if OPENRAG_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {OPENRAG_AUTH_TOKEN}"
    last_error = "unknown error"
    # One client (and connection pool) for all attempts — created outside the retry
    # loop rather than re-allocated per attempt.
    async with httpx.AsyncClient(timeout=CONFIG.question_gen.fetch_timeout) as client:
        for attempt in range(retries):
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                # resp.json() parses the full (~1.7GB) body — heavy CPU. Run it off
                # the event loop so it never blocks a host loop if embedded.
                payload = await asyncio.to_thread(resp.json)
                all_chunks_list = payload.get("chunks")
                if not all_chunks_list:
                    # Reachable + authorised, but nothing indexed — retrying won't help.
                    raise ChunkFetchError(
                        f"Partition returned no chunks — it is empty or not indexed yet ({url})."
                    )
                return all_chunks_list
            except ChunkFetchError:
                raise  # already-classified definitive error; don't retry or wrap
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (401, 403):
                    raise ChunkFetchError(
                        f"Authentication failed (HTTP {code}) for {url}. "
                        "Set a valid AUTH_TOKEN (Bearer token) for the OpenRAG server."
                    ) from e
                if code == 404:
                    raise ChunkFetchError(
                        f"Partition not found (HTTP 404) at {url}. "
                        "Check the --partition name and that it exists on the server."
                    ) from e
                last_error = f"HTTP {code}"  # 5xx / other: transient, retry
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_error = f"cannot reach server ({type(e).__name__})"
            except Exception as e:  # noqa: BLE001 — record and retry any other transient error
                last_error = f"{type(e).__name__}: {e}"
            logger.debug(f"Attempt {attempt + 1}/{retries} failed: {last_error}")
            if attempt < retries - 1:
                await asyncio.sleep(1)  # Wait before retrying
    raise ChunkFetchError(
        f"Could not fetch chunks from {url} after {retries} attempts ({last_error}). "
        "Check the server is running, the partition name, and AUTH_TOKEN."
    )


def _pick_type(rng: random.Random) -> str:
    """Sample a question type from the configured distribution (seeded)."""
    dist = CONFIG.question_gen.typing.distribution
    types = list(dist.keys())
    weights = list(dist.values())
    return rng.choices(types, weights=weights, k=1)[0]


async def _run_safe(coro, qid: str):
    """Run one generator coroutine, converting a post-retry hard failure into a
    dropped item (None) instead of aborting the whole ``tqdm.gather`` batch.

    ``llm.with_retry()`` already absorbs transient hiccups; this guards the residual
    cases (prolonged outage, context-length / token-limit breach, hard validation
    error) so a single failed item can't discard every completed item in the run —
    ``tqdm.gather`` uses ``return_exceptions=False``, so an uncaught exception would
    propagate out and crash main(). ``asyncio.CancelledError`` derives from
    BaseException and is intentionally NOT caught, so shutdown still propagates.
    """
    try:
        return await coro
    except Exception as e:
        logger.warning(
            f"Generation task {qid} failed after retries; dropping it "
            f"({type(e).__name__}: {e})"
        )
        return None


# ============================================================================
# Capability-suite profile (second generation method). Fixed count per capability,
# each drawing from capability-appropriate chunks. Reuses generate_qa /
# generate_unanswerable (critic, self-containment gate, metadata) unchanged.
# ============================================================================
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[ :]*-{3,}[ :]*(\|[ :]*-{2,}[ :]*)+\|?\s*$", re.MULTILINE)
_NUMERIC_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d[\d.,]*")


def _has_table(text: str) -> bool:
    """True if the chunk body contains a markdown table (a separator row, or >=3
    pipe-delimited rows)."""
    body = utils.chunk_body(text)
    if _TABLE_SEPARATOR_RE.search(body):
        return True
    return sum(1 for ln in body.splitlines() if ln.count("|") >= 2) >= 3


def _numeric_token_count(text: str) -> int:
    return len(_NUMERIC_TOKEN_RE.findall(utils.chunk_body(text)))


def _sample_for_capability(category, rng, pools, cfg):
    """Pick a chunk-set appropriate to ``category``, or None if the pool can't
    supply it. ``pools`` = {all, table, numeric, by_file}."""
    all_chunks, table, numeric, by_file = (
        pools["all"], pools["table"], pools["numeric"], pools["by_file"],
    )
    if category == "table_lookup":
        return [rng.choice(table)] if table else None
    if category == "numerical_reasoning":
        pool = numeric or all_chunks
        n = min(rng.randint(1, 2), len(pool))
        return rng.sample(pool, n) if pool else None
    if category == "cross_document_reasoning":
        # One chunk from each of >=2 distinct files.
        if len(by_file) < 2:
            return None
        files = rng.sample(list(by_file), rng.randint(2, min(3, len(by_file))))
        return [rng.choice(by_file[f]) for f in files]
    if category == "multi_hop_retrieval":
        if len(all_chunks) < 2:
            return None
        return rng.sample(all_chunks, rng.randint(2, min(3, len(all_chunks))))
    if category == "long_context_retrieval":
        lo, hi = cfg.long_context_min_chunks, cfg.long_context_max_chunks
        if len(all_chunks) < lo:
            return None
        return rng.sample(all_chunks, rng.randint(lo, min(hi, len(all_chunks))))
    # citation_grounding / unanswerable / fallback: 1–2 arbitrary content chunks.
    n = min(rng.randint(1, 2), len(all_chunks))
    return rng.sample(all_chunks, n) if all_chunks else None


def _capability_pool_ok(category, pools, cfg) -> tuple[bool, str]:
    """Whether ``category`` can be generated at all from the available chunks."""
    if category == "table_lookup" and not pools["table"]:
        return False, "no chunks containing tables"
    if category == "cross_document_reasoning" and len(pools["by_file"]) < 2:
        return False, "corpus has < 2 distinct files"
    if category in ("multi_hop_retrieval",) and len(pools["all"]) < 2:
        return False, "< 2 chunks available"
    if category == "long_context_retrieval" and len(pools["all"]) < cfg.long_context_min_chunks:
        return False, f"< {cfg.long_context_min_chunks} chunks available"
    return True, ""


async def generate_capability_suite(all_chunks: list[dict], semaphore, ctx: GenCtx):
    """Generate a fixed number of questions per capability (loop-until-count).

    Each round over-generates (``oversample``) to absorb critic / self-containment
    rejections, keeping the first N that PASS. Reuses generate_qa /
    generate_unanswerable, so every item gets the same quality gating and metadata.
    """
    cfg = CONFIG.question_gen.capability_suite
    rng = random.Random(CONFIG.question_gen.seed)
    pools = {
        "all": all_chunks,
        "table": [c for c in all_chunks if _has_table(c["text"])],
        "numeric": [c for c in all_chunks if _numeric_token_count(c["text"]) >= cfg.numeric_min_tokens],
        "by_file": {},
    }
    for c in all_chunks:
        pools["by_file"].setdefault(c["file_id"], []).append(c)
    logger.info(
        f"Capability pools: {len(pools['all'])} chunks, {len(pools['table'])} with tables, "
        f"{len(pools['numeric'])} number-rich, {len(pools['by_file'])} distinct files."
    )

    results: list[dict] = []
    counter = 0
    for category in cfg.enabled_categories:
        target = cfg.count_overrides.get(category, cfg.per_category)
        if target <= 0:
            continue
        ok, why = _capability_pool_ok(category, pools, cfg)
        if not ok:
            logger.warning(f"Capability '{category}': skipping (0/{target}) — {why}.")
            continue

        got = 0
        for _round in range(cfg.max_rounds):
            if got >= target:
                break
            need = target - got
            n_tasks = max(1, math.ceil(need * cfg.oversample))
            tasks = []
            for _ in range(n_tasks):
                sample = _sample_for_capability(category, rng, pools, cfg)
                if sample is None:
                    continue
                counter += 1
                qid = f"q-{ctx.partition}-cap-{counter:06d}"
                if category == "unanswerable":
                    coro = generate_unanswerable(
                        sample, semaphore, qid=qid, cluster_id=None, ctx=ctx
                    )
                else:
                    coro = generate_qa(
                        sample, semaphore, qid=qid, cluster_id=None, qtype=category, ctx=ctx
                    )
                tasks.append(_run_safe(coro, qid))
            if not tasks:
                break
            batch = await tqdm.gather(*tasks, desc=f"{category} ({got}/{target})")
            for item in batch:
                if got >= target:
                    break
                # Only PASSING items count toward the target (capability suite is
                # target-driven; rejects are discarded rather than flagged/kept).
                if item and item["metadata"]["quality"]["passed"]:
                    results.append(item)
                    got += 1

        if got < target:
            logger.warning(
                f"Capability '{category}': {got}/{target} after {cfg.max_rounds} rounds "
                "(raise capability_suite.max_rounds/oversample, or the pool is too small)."
            )
        else:
            logger.info(f"Capability '{category}': {got}/{target} generated.")
    return results


async def generate_questions_from_clusters(
    clusters: dict,
    semaphore: asyncio.Semaphore,
    ctx: GenCtx,
    n_min=1,
    n_max=2,
    n_questions_per_cluster=3,
    n_unanswerable_per_cluster=1,
):
    typing_on = CONFIG.question_gen.typing.enabled
    # Dedicated, independent RNGs (not the process-global `random`) so generation is
    # reproducible without mutating global state other code in the process might rely
    # on. Two separate streams seeded from the same config seed — type sampling and
    # chunk sampling never advance each other, reproducing the prior behaviour where
    # the global RNG (seeded once in main) was used exclusively for chunk sampling.
    type_rng = random.Random(CONFIG.question_gen.seed)
    chunk_rng = random.Random(CONFIG.question_gen.seed)

    # Build deterministic task specs first so each item gets a stable, reproducible
    # id assigned in creation order (independent of concurrent execution order).
    specs = []
    counter = 0
    for cluster_label, chunks in clusters.items():
        for _ in range(n_questions_per_cluster):
            # typing off -> "generic": generate_qa maps it to the generic instruction
            # and (via fallback) the single-hop spec, matching the pre-decoupling path.
            qtype = _pick_type(type_rng) if typing_on else "generic"
            spec = QUESTION_TYPE_SPEC.get(qtype, QUESTION_TYPE_SPEC[DEFAULT_QUESTION_TYPE])
            min_chunks = spec["min_chunks"]
            # Multi-hop/comparative types need ≥2 chunks; if the cluster is too
            # small, fall back to single-hop rather than failing the sample.
            if len(chunks) < min_chunks:
                qtype = DEFAULT_QUESTION_TYPE
                spec = QUESTION_TYPE_SPEC[DEFAULT_QUESTION_TYPE]
                min_chunks = 1
            if spec["multi_hop"]:
                hi = min(n_max, len(chunks))
                lo = min(max(n_min, min_chunks), hi)
                n = chunk_rng.randint(lo, hi)
            else:
                # single_hop_specific / reasoning are generated with an explicit
                # "answerable from a SINGLE document alone" instruction (see
                # QUESTION_TYPE_INSTRUCTIONS_EN) — sampling extra chunks here would
                # attach them as "gold" ground truth the question never actually
                # needs. The answerable critic only checks answerability against
                # the union of sampled chunks, not that each one is individually
                # necessary, so it can't catch this after the fact. Keep n at
                # exactly min_chunks (1) so ground truth matches what the LLM was
                # actually asked to do.
                n = min_chunks
            sampled_chunks = chunk_rng.sample(chunks, n)
            qid = f"q-{ctx.partition}-{counter:06d}"
            counter += 1
            specs.append(("answerable", qid, cluster_label, qtype, sampled_chunks))
        for _ in range(n_unanswerable_per_cluster):
            n = chunk_rng.randint(n_min, min(n_max, len(chunks)))
            sampled_chunks = chunk_rng.sample(chunks, n)
            qid = f"q-{ctx.partition}-{counter:06d}"
            counter += 1
            specs.append(("unanswerable", qid, cluster_label, None, sampled_chunks))

    tasks = []
    for kind, qid, cluster_label, qtype, sampled_chunks in specs:
        if kind == "answerable":
            coro = generate_qa(
                chunks=sampled_chunks,
                semaphore=semaphore,
                qid=qid,
                cluster_id=cluster_label,
                qtype=qtype,
                ctx=ctx,
            )
        else:
            coro = generate_unanswerable(
                chunks=sampled_chunks,
                semaphore=semaphore,
                qid=qid,
                cluster_id=cluster_label,
                ctx=ctx,
            )
        # Isolate each task so one hard failure can't abort the whole gather batch.
        tasks.append(_run_safe(coro, qid))

    questions_and_answers = await tqdm.gather(*tasks, desc="Question and Answer Generation...")
    return questions_and_answers


async def _annotate_document_categories(questions: list[dict], all_chunks: list[dict], ctx: GenCtx):
    """Fill metadata.source.document_category on every generated item.

    Baked into the dataset at build time (rather than recomputed per benchmark run)
    so each dataset carries stable labels: every eval over it — including the
    orchestrator's multi-version comparisons — slices on identical categories.
    """
    qg = CONFIG.question_gen
    used = {n for q in questions for n in (q["metadata"]["source"].get("source_filenames") or [])}
    if not used:
        return
    texts = {}
    for c in all_chunks:
        name = utils.filename_of(c)
        if name in used and name not in texts:
            texts[name] = utils.chunk_body(c["text"])
    categories = await utils.resolve_document_categories(
        texts,
        utils.category_cache_path(ctx.partition),
        _classifier_ainvoke,
        enabled=qg.classify_document_categories,
        hints=qg.document_category_hints,
        max_labels=qg.document_category_max_labels,
        sample_docs=qg.document_category_sample_docs,
        sample_chars=qg.document_category_sample_chars,
        batch_size=qg.document_category_batch_size,
    )
    for q in questions:
        cats = sorted(
            {categories.get(n, "unknown") for n in (q["metadata"]["source"].get("source_filenames") or [])}
        )
        q["metadata"]["source"]["document_category"] = (
            cats[0] if len(cats) == 1 else ("mixed" if cats else "unknown")
        )


def _write_manifest(output_path: str, ctx: GenCtx, questions: list[dict], counts: dict):
    """Write run-level provenance to a sidecar manifest next to the dataset.

    The dataset file itself must remain a top-level JSON *list* (benchmark.py
    rejects anything else), so run-level metadata lives here. Each item's
    metadata.generation.run_id joins back to this manifest.
    """
    base, _ = os.path.splitext(output_path)
    manifest_path = f"{base}.manifest.json"
    qg = CONFIG.question_gen
    manifest = {
        "run_id": ctx.run_id,
        "created_at": _utc_now_iso(),
        "schema_version": qg.schema_version,
        "prompt_version": qg.prompt_version,
        "partition": ctx.partition,
        "generator_model": MODEL,
        "base_url": BASE_URL,
        # Which model produced the quality verdicts, and whether it self-judged.
        "critic_model": CRITIC_MODEL,
        "critic_base_url": CRITIC_BASE_URL,
        "critic_is_generator": CRITIC_MODEL == MODEL and CRITIC_BASE_URL == BASE_URL,
        "language": ctx.language,
        "profile": qg.profile,
        "clustering": {"method": ctx.clustering_method, **asdict(qg.clustering)},
        "generation_params": {
            "temperature": qg.temperature,
            "seed": qg.seed,
            "n_min": qg.n_min,
            "n_max": qg.n_max,
            "n_questions_per_cluster": qg.n_questions_per_cluster,
            "n_unanswerable_per_cluster": qg.n_unanswerable_per_cluster,
        },
        "filtration": asdict(qg.filtration),
        "typing": asdict(qg.typing),
        "chunk_filter": asdict(qg.chunk_filter),
        "capability_suite": asdict(qg.capability_suite),
        "counts": counts,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=4)
    logger.info(f"Wrote run manifest to {manifest_path}")


def _build_embeddings(chunk_embeddings: list[str]) -> np.ndarray:
    """Parse the string-encoded vectors (e.g. "[0.1, -0.2, ...]") into a compact
    float32 matrix.

    json.loads is ~5x faster than ast.literal_eval here, and float32 halves the
    array's memory. CPU-bound, so main() offloads it via ``asyncio.to_thread``.
    """
    return np.array([json.loads(v) for v in chunk_embeddings], dtype=np.float32)


def _reduce_and_cluster(embeddings: np.ndarray, clustering_method: str) -> np.ndarray:
    """UMAP dimensionality reduction followed by clustering; returns per-chunk labels.

    Pure CPU-bound work — UMAP / NumPy / scikit-learn run in C extensions that
    release the GIL — so main() runs this via ``asyncio.to_thread``. That's a no-op
    for the standalone CLI (nothing else is on the loop yet), but keeps a host loop
    responsive if this module is ever driven from inside an async application.
    """
    N = len(embeddings)

    # Tiny partition: UMAP can't reduce to n_components dims with barely more points
    # than dims, and HDBSCAN rejects min_cluster_size < 2 / min_samples < 1. Skip
    # reduction + clustering entirely and treat the whole partition as one cluster,
    # so a small corpus still yields questions instead of crashing.
    min_clusterable_n = CONFIG.question_gen.clustering.umap_n_components + 2
    if N < min_clusterable_n:
        logger.warning(
            f"Only {N} chunk(s) in the partition (< {min_clusterable_n}); skipping "
            "UMAP + clustering and using a single cluster of all chunks."
        )
        return np.zeros(N, dtype=int)

    # Reduce the embedding vectors' dimensionality with UMAP, then cluster.
    reducer = umap.UMAP(
        n_neighbors=CONFIG.question_gen.clustering.umap_n_neighbors,
        n_components=CONFIG.question_gen.clustering.umap_n_components,
        min_dist=CONFIG.question_gen.clustering.umap_min_dist,
        metric=CONFIG.question_gen.clustering.umap_metric,
        random_state=CONFIG.question_gen.clustering.umap_random_state,
    )
    embeddings = reducer.fit_transform(embeddings)

    # Choose clustering algorithm: "hdbscan" | "kmeans" | "dbscan"
    if clustering_method == "hdbscan":
        # HDBSCAN: density-based, auto-detects cluster count, labels noise as -1.
        # Floor the dynamic params to HDBSCAN's minimums (min_cluster_size >= 2,
        # min_samples >= 1) so small-but-clusterable partitions don't raise.
        min_cluster_size = max(2, int(np.sqrt(N / 2)))
        min_samples = max(1, int(np.log(N)))
        hdb = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples)
        return hdb.fit_predict(embeddings)
    if clustering_method == "kmeans":
        # KMeans: exact cluster count, no noise label.
        # K = sqrt(N) is a common rule-of-thumb; adjust for more/fewer clusters.
        K = max(2, int(np.sqrt(N)))
        kmeans = KMeans(
            n_clusters=K,
            random_state=CONFIG.question_gen.clustering.kmeans_random_state,
            n_init=CONFIG.question_gen.clustering.kmeans_n_init,
        )
        labels = kmeans.fit_predict(embeddings)
        logger.info(f"KMeans: K={K} clusters on N={N} points")
        return labels
    if clustering_method == "dbscan":
        db = DBSCAN(
            eps=CONFIG.question_gen.clustering.dbscan_eps,
            min_samples=CONFIG.question_gen.clustering.dbscan_min_samples,
            metric=CONFIG.question_gen.clustering.dbscan_metric,
        )
        return db.fit_predict(embeddings)
    raise ValueError(f"Unknown clustering_method: {clustering_method}")


async def main(
    partition: str = CONFIG.common.partition,
    n_questions_per_cluster: int = CONFIG.question_gen.n_questions_per_cluster,
    n_unanswerable_per_cluster: int = CONFIG.question_gen.n_unanswerable_per_cluster,
    output: str | None = None,
):
    # Sampling RNGs are seeded locally in generate_questions_from_clusters (no global
    # random.seed — it would mutate process-wide state). UMAP is seeded separately via
    # clustering.umap_random_state.
    openrag_api_base_url = os.environ.get("API_BASE_URL")
    url = f"{openrag_api_base_url}/partition/{partition}/chunks"

    start = time.time()
    # get_all_chunks returns a non-empty list or raises ChunkFetchError with a
    # clear cause (auth / partition-not-found / unreachable / empty), handled cleanly
    # in __main__ — no generic "no chunks" guard needed here.
    all_chunks_list = await get_all_chunks(url)
    pause = time.time()
    logger.info(f"Clusters retrieval time: {pause - start} seconds")

    ids, chunk_contents, chunk_embeddings, file_ids = map(
        list,
        zip(
            *[
                (
                    chunk["metadata"]["_id"],
                    chunk["content"],
                    chunk["metadata"]["vector"],
                    chunk["metadata"]["file_id"],
                )
                for chunk in all_chunks_list
            ]
        ),
    )

    # Drop low-information chunks (bibliographies, acknowledgements, boilerplate)
    # BEFORE embedding: they yield metadata trivia instead of questions that test
    # retrieval, and they'd otherwise skew UMAP/clustering too.
    n_chunks_total = len(ids)
    filter_reasons: dict[str, int] = {}
    if CONFIG.question_gen.chunk_filter.enabled:
        keep = []
        for i, text in enumerate(chunk_contents):
            reason = _chunk_quality_reason(text)
            if reason:
                filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
            else:
                keep.append(i)
        if not keep:
            raise RuntimeError(
                f"Chunk filter dropped all {n_chunks_total} chunks ({filter_reasons}) — "
                "nothing to generate from; loosen question_gen.chunk_filter."
            )
        ids = [ids[i] for i in keep]
        chunk_contents = [chunk_contents[i] for i in keep]
        chunk_embeddings = [chunk_embeddings[i] for i in keep]
        file_ids = [file_ids[i] for i in keep]
        logger.info(
            f"Chunk filter: kept {len(keep)}/{n_chunks_total} chunks "
            f"(dropped {n_chunks_total - len(keep)}: {filter_reasons or 'none'})"
        )

    # ---- language-dependent instructions + run context (used by both profiles) ----
    # The only language-dependent pieces are two instruction strings, injected into
    # the shared generators. (System prompts are English regardless.)
    if CONFIG.question_gen.language == "fr":
        answer_instruction = "Generate the answer in the same language as the documents."
        unanswerable_instruction = (
            "Now generate ONE adversarial question as described, whose answer "
            "is NOT present in the context. Maximum 30 words."
        )
    else:
        answer_instruction = "Generate the answer in English."
        unanswerable_instruction = (
            "Now generate a topic-adjacent question whose answer is NOT in the provided chunks. Maximum 30 words."
        )

    profile = CONFIG.question_gen.profile
    clustering_method = CONFIG.question_gen.clustering.method
    ctx = GenCtx(
        partition=partition,
        clustering_method=clustering_method if profile == "distribution" else "none",
        run_id="gen-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        language=CONFIG.question_gen.language,
        answer_instruction=answer_instruction,
        unanswerable_instruction=unanswerable_instruction,
    )
    # One shared semaphore, created inside the running loop (not at import time),
    # caps concurrent generator-LLM calls across every task.
    gen_semaphore = asyncio.Semaphore(CONFIG.question_gen.gen_concurrency)

    if profile == "capability_suite":
        # Capabilities select chunks by content (tables, numbers, distinct files),
        # not by topic — so clustering/embedding is skipped entirely here.
        all_chunks = [
            {"id": ids[i], "text": chunk_contents[i], "file_id": file_ids[i]}
            for i in range(len(ids))
        ]
        del all_chunks_list, chunk_embeddings  # free the ~1.7GB raw response + vectors
        logger.info(
            f"Profile 'capability_suite': generating over {len(all_chunks)} chunks "
            "(clustering skipped)."
        )
        questions = await generate_capability_suite(all_chunks, gen_semaphore, ctx)
    else:
        # Distribution profile: embed -> UMAP + cluster -> per-cluster generation.
        # Both steps are heavy synchronous CPU work; offload to a worker thread so
        # they never block the event loop (NumPy/sklearn/UMAP release the GIL).
        embeddings = await asyncio.to_thread(_build_embeddings, chunk_embeddings)
        del all_chunks_list, chunk_embeddings  # free the ~1.7GB raw response + vectors
        N = len(embeddings)
        labels = await asyncio.to_thread(_reduce_and_cluster, embeddings, clustering_method)

        clusters = {}
        for idx, label in enumerate(labels):
            if label == -1:
                continue  # -1 == noise
            clusters.setdefault(int(label), []).append(
                {"id": ids[idx], "text": chunk_contents[idx], "file_id": file_ids[idx]}
            )

        # Safety net: all-noise -> single cluster of everything, so generation still
        # has input instead of writing an empty dataset.
        if not clusters:
            logger.warning(
                "Clustering produced no clusters (all points were noise); "
                "falling back to a single cluster of all chunks."
            )
            clusters = {
                0: [
                    {"id": ids[i], "text": chunk_contents[i], "file_id": file_ids[i]}
                    for i in range(N)
                ]
            }

        for label, items in clusters.items():
            logger.info(f"Cluster {label}: {[item['id'] for item in items]}")

        questions = await generate_questions_from_clusters(
            clusters,
            semaphore=gen_semaphore,
            ctx=ctx,
            n_min=CONFIG.question_gen.n_min,
            n_max=CONFIG.question_gen.n_max,
            n_questions_per_cluster=n_questions_per_cluster,
            n_unanswerable_per_cluster=n_unanswerable_per_cluster,
        )

    # Drop items whose generation hard-failed after retries (see _run_safe); these
    # are distinct from the critic/filtration rejections handled just below (which
    # keep their item, flagged). A total wipeout means the LLM/API is down — fail
    # loudly rather than silently writing an empty dataset.
    n_gen_errors = sum(1 for q in questions if q is None)
    if n_gen_errors:
        logger.warning(f"{n_gen_errors} generation task(s) failed after retries and were dropped.")
        questions = [q for q in questions if q is not None]
    if not questions:
        raise RuntimeError(
            "All generation tasks failed (likely an LLM/API outage) — no dataset written."
        )

    # Optionally drop items the critic never accepted (gap 1: quality filtering).
    n_failed = sum(1 for q in questions if not q["metadata"]["quality"]["passed"])
    if CONFIG.question_gen.filtration.enabled and CONFIG.question_gen.filtration.drop_failed:
        kept = [q for q in questions if q["metadata"]["quality"]["passed"]]
        logger.info(f"Filtration: dropped {len(questions) - len(kept)} item(s) that failed the critic.")
        questions = kept
    elif n_failed:
        logger.warning(
            f"Filtration: {n_failed} item(s) failed the critic but were kept "
            "(metadata.quality.passed=False); set filtration.drop_failed=True to exclude them."
        )

    # Stamp the content-domain category onto every item (cache > filename rule > LLM).
    await _annotate_document_categories(
        questions,
        [{"id": ids[i], "text": chunk_contents[i], "file_id": file_ids[i]} for i in range(len(ids))],
        ctx,
    )

    n_unanswerable = sum(1 for q in questions if not q.get("answerable", True))
    n_answerable = len(questions) - n_unanswerable
    by_type: dict[str, int] = {}
    for q in questions:
        t = q["metadata"]["question_type"]
        by_type[t] = by_type.get(t, 0) + 1
    logger.info(
        f"Questions generated time: ({time.time() - pause}) seconds — "
        f"{n_answerable} answerable, {n_unanswerable} unanswerable | by type: {by_type}"
    )

    output_path = output or CONFIG.common.dataset_path
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=4)
    logger.info(f"Wrote {len(questions)} questions to {output_path}")

    _write_manifest(
        output_path,
        ctx,
        questions,
        counts={
            "total": len(questions),
            "answerable": n_answerable,
            "unanswerable": n_unanswerable,
            "by_question_type": by_type,
            "failed_critic": n_failed,
            "generation_errors": n_gen_errors,
            "chunks_total": n_chunks_total,
            "chunks_kept": len(ids),
            "chunks_dropped_by_reason": filter_reasons,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a Q&A evaluation dataset from a partition's chunks."
    )
    parser.add_argument(
        "--partition", default=CONFIG.common.partition, help="OpenRAG partition to read chunks from"
    )
    parser.add_argument(
        "--n-questions-per-cluster",
        type=int,
        default=CONFIG.question_gen.n_questions_per_cluster,
        help="Answerable questions to generate per cluster",
    )
    parser.add_argument(
        "--n-unanswerable-per-cluster",
        type=int,
        default=CONFIG.question_gen.n_unanswerable_per_cluster,
        help="Unanswerable questions to generate per cluster",
    )
    parser.add_argument(
        "--output",
        default=CONFIG.common.dataset_path,
        help="Where to write the generated dataset JSON (default: ./dataset.json).",
    )
    parser.add_argument(
        "--no-filtration",
        action="store_true",
        help="Disable the LLM-critic quality gate (overrides config).",
    )
    parser.add_argument(
        "--no-typing",
        action="store_true",
        help="Disable typed-question diversity; use the generic question prompt (overrides config).",
    )
    parser.add_argument(
        "--profile",
        choices=["distribution", "capability_suite"],
        default=CONFIG.question_gen.profile,
        help="Generation profile: 'distribution' (cluster + type mix, default) or "
        "'capability_suite' (fixed count per capability). Overrides config.",
    )
    parser.add_argument(
        "--per-category",
        type=int,
        default=None,
        help="capability_suite only: questions per capability (default from config, 50).",
    )
    args = parser.parse_args()

    if args.no_filtration:
        CONFIG.question_gen.filtration.enabled = False
    if args.no_typing:
        CONFIG.question_gen.typing.enabled = False
    CONFIG.question_gen.profile = args.profile
    if args.per_category is not None:
        CONFIG.question_gen.capability_suite.per_category = args.per_category

    try:
        asyncio.run(
            main(
                partition=args.partition,
                n_questions_per_cluster=args.n_questions_per_cluster,
                n_unanswerable_per_cluster=args.n_unanswerable_per_cluster,
                output=args.output,
            )
        )
    except ChunkFetchError as e:
        # Expected operational failure (bad token / partition / server down): exit
        # cleanly with the cause and a non-zero code, not a Python traceback.
        logger.error(str(e))
        sys.exit(1)
