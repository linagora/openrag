import argparse
import asyncio
import json
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
from config import CONFIG
from dotenv import load_dotenv
from evaluation_prompts import (
    ANSWER_TMPL_EN,
    ANSWERABLE_CRITIC_PROMPT_EN,
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

# Critic LLM for the filtration pass. Same endpoint/model, but low temperature for
# deterministic scoring. The critic is prompted to emit strict JSON and parsed
# defensively (no native structured-output / function-calling dependency), so it
# works against any OpenAI-compatible proxy.
critic_settings = {**settings, "temperature": CONFIG.question_gen.filtration.critic_temperature}
critic_llm = ChatOpenAI(**critic_settings).with_retry(
    stop_after_attempt=CONFIG.question_gen.stop_after_attempt
)

# Per-type generation spec: difficulty label, minimum chunks required, and whether
# the type is inherently multi-hop. Keys MUST match
# QUESTION_TYPE_INSTRUCTIONS_EN / config.question_gen.typing.distribution.
QUESTION_TYPE_SPEC = {
    "single_hop_specific": {"difficulty": "easy", "min_chunks": 1, "multi_hop": False},
    "multi_hop_specific": {"difficulty": "hard", "min_chunks": 2, "multi_hop": True},
    "reasoning": {"difficulty": "medium", "min_chunks": 1, "multi_hop": False},
    "comparative": {"difficulty": "medium", "min_chunks": 2, "multi_hop": True},
}
DEFAULT_QUESTION_TYPE = "single_hop_specific"


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

    Mirrors the defensive judge-parsing in benchmark.py: strip markdown fences,
    lenient-parse, then fall back to extracting the first {...} block. Returns
    None when nothing parseable is found (caller treats that as 'cannot verify').
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text, strict=False)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0), strict=False)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


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


def _reasons(verdict: dict | None, passed: bool) -> list[str]:
    if passed:
        return []
    if verdict is None:
        return ["critic_unparseable"]
    reasons = verdict.get("reasons") or []
    return [str(r) for r in reasons] if isinstance(reasons, list) else [str(reasons)]


def _build_source(chunks: list[dict], cluster_id, ctx: GenCtx) -> dict:
    return {
        "partition": ctx.partition,
        "cluster_id": cluster_id,
        "clustering_method": ctx.clustering_method,
        "source_file_ids": sorted({c["file_id"] for c in chunks}),
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
    spec = QUESTION_TYPE_SPEC.get(qtype, QUESTION_TYPE_SPEC[DEFAULT_QUESTION_TYPE])
    typing_on = CONFIG.question_gen.typing.enabled
    type_instruction = (
        QUESTION_TYPE_INSTRUCTIONS_EN.get(qtype, GENERIC_QUESTION_INSTRUCTION_EN)
        if typing_on
        else GENERIC_QUESTION_INSTRUCTION_EN
    )
    filt = CONFIG.question_gen.filtration
    max_attempts = (filt.max_quality_retries + 1) if filt.enabled else 1

    async with semaphore:
        t0 = time.perf_counter()
        prompt_tok = comp_tok = 0
        chunks_str = format_chunks([c["text"] for c in chunks])

        llm_question = llm_answer = ""
        verdict: dict | None = None
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

            # 3) critic / filtration
            if not filt.enabled:
                break
            verdict, p, c = await _critic_answerable(chunks_str, llm_question, llm_answer)
            prompt_tok += p
            comp_tok += c
            if _answerable_passed(verdict):
                break

        passed = _answerable_passed(verdict) if filt.enabled else True
        n_hops = len(chunks) if spec["multi_hop"] else 1
        return {
            "id": qid,
            "question": llm_question,
            "chunks": chunks,
            "llm_answer": llm_answer,
            "answerable": True,
            "metadata": {
                "schema_version": CONFIG.question_gen.schema_version,
                "question_type": qtype if typing_on else "generic",
                "difficulty": spec["difficulty"],
                "n_hops": n_hops,
                "evolution": ["base", qtype] if typing_on else ["base"],
                "language": ctx.language,
                "persona": None,
                "source": _build_source(chunks, cluster_id, ctx),
                "quality": {
                    "answerability": (verdict or {}).get("answerability"),
                    "self_containment": (verdict or {}).get("self_containment"),
                    "clarity": (verdict or {}).get("clarity"),
                    "faithfulness_verified": (verdict or {}).get("faithful"),
                    "unanswerable_verified": None,
                    "passed": passed,
                    "regen_attempts": attempt,
                    "reject_reasons": _reasons(verdict, passed),
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
            verdict, p, c = await _critic_unanswerable(chunks_str, llm_question)
            prompt_tok += p
            comp_tok += c
            if _unanswerable_passed(verdict):
                break

        passed = _unanswerable_passed(verdict) if filt.enabled else True
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
                    "reject_reasons": _reasons(verdict, passed),
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
            qtype = _pick_type(type_rng) if typing_on else DEFAULT_QUESTION_TYPE
            min_chunks = QUESTION_TYPE_SPEC.get(qtype, {}).get("min_chunks", 1)
            # Multi-hop/comparative types need ≥2 chunks; if the cluster is too
            # small, fall back to single-hop rather than failing the sample.
            if len(chunks) < min_chunks:
                qtype = DEFAULT_QUESTION_TYPE
                min_chunks = 1
            hi = min(n_max, len(chunks))
            lo = min(max(n_min, min_chunks), hi)
            n = chunk_rng.randint(lo, hi)
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
        "language": ctx.language,
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

    # Parse the string-encoded vectors into a compact float32 matrix. CPU-bound, so
    # run it off the event loop (keeps a host loop responsive if this is embedded).
    embeddings = await asyncio.to_thread(_build_embeddings, chunk_embeddings)

    # Free the raw JSON response (~1.7GB on large partitions) and the string-encoded
    # vectors now that the compact float32 array exists — neither is referenced again,
    # so this keeps them from sitting resident through clustering + generation.
    del all_chunks_list, chunk_embeddings

    N = len(embeddings)
    clustering_method = CONFIG.question_gen.clustering.method

    # UMAP reduction + clustering are heavy synchronous CPU work; offload to a worker
    # thread so they never block the event loop (NumPy/sklearn/UMAP release the GIL).
    labels = await asyncio.to_thread(_reduce_and_cluster, embeddings, clustering_method)

    clusters = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue  # -1 == noise
        clusters.setdefault(int(label), []).append(
            {
                "id": ids[idx],
                "text": chunk_contents[idx],
                "file_id": file_ids[idx],
            }
        )

    # Safety net: if every point was labelled noise (HDBSCAN/DBSCAN can do this on
    # small or sparse partitions), fall back to a single cluster of all chunks so
    # generation still has input instead of writing an empty dataset.
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

    ctx = GenCtx(
        partition=partition,
        clustering_method=clustering_method,
        run_id="gen-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        language=CONFIG.question_gen.language,
        answer_instruction=answer_instruction,
        unanswerable_instruction=unanswerable_instruction,
    )

    # One shared semaphore, created inside the running loop (not at import time),
    # caps concurrent generator-LLM calls across answerable + unanswerable tasks.
    gen_semaphore = asyncio.Semaphore(CONFIG.question_gen.gen_concurrency)
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

    n_unanswerable = sum(1 for q in questions if not q.get("answerable", True))
    n_answerable = len(questions) - n_unanswerable
    logger.info(
        f"Questions generated time: ({time.time() - pause}) seconds — "
        f"{n_answerable} answerable, {n_unanswerable} unanswerable"
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
            "failed_critic": n_failed,
            "generation_errors": n_gen_errors,
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
    args = parser.parse_args()

    if args.no_filtration:
        CONFIG.question_gen.filtration.enabled = False
    if args.no_typing:
        CONFIG.question_gen.typing.enabled = False

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
