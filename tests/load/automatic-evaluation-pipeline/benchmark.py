import argparse
import asyncio
import json
import math
import os
import random
import re
import time
from datetime import datetime
from typing import Literal, TypedDict

import httpx
import numpy as np
import pandas as pd
from config import CONFIG
from dotenv import load_dotenv
from evaluation_prompts import (
    ANSWER_RELEVANCY_JUDGE_PROMPT_EN,
    COMPLETION_JUDGE_PROMPT_COT,
    COMPLETION_JUDGE_PROMPT_COT_EN,
    COMPLETION_JUDGE_PROMPT_EN,
    CONTEXT_RELEVANCY_JUDGE_PROMPT_EN,
    FAITHFULNESS_JUDGE_PROMPT_EN,
    LABEL_JUDGE_PROMPT,
    LABEL_JUDGE_PROMPT_EN,
    PRECISION_JUDGE_PROMPT_COT,
    PRECISION_JUDGE_PROMPT_COT_EN,
    PRECISION_JUDGE_PROMPT_EN,
    REFUSAL_JUDGE_PROMPT_EN,
)
from judge_schemas import (
    RESPONSE_LABELS,
    AnswerRelevancyEvaluationResponse,
    CompletionEvaluationCoTResponse,
    CompletionEvaluationCoTResponseEN,
    CompletionEvaluationResponse,
    ContextRelevancyEvaluationResponse,
    FaithfulnessEvaluationResponse,
    PrecisionEvaluationCoTResponse,
    PrecisionEvaluationCoTResponseEN,
    PrecisionEvaluationResponse,
    RefusalEvaluationResponse,
    ResponseLabelResponse,
)
from langchain_core.utils.json import parse_partial_json
from langchain_openai import ChatOpenAI
from loguru import logger
from metrics import (
    average_precision,
    compute_generation_metrics,
    compute_hit_at_k,
    compute_mrr,
    ndcg_at_k,
    precision_at_k,
    r_precision,
    recall_at_k,
)
from openai import (
    APIConnectionError,
    AsyncOpenAI,
    AuthenticationError,
    LengthFinishReasonError,
    NotFoundError,
    PermissionDeniedError,
)
from tqdm.asyncio import tqdm

load_dotenv()


# Retrieving responses and document sources fom OpenRAG
async def retrieve_response_and_docs_openrag(
    query: str, partition: str, _base_url: str, semaphore: asyncio.Semaphore
):
    async with semaphore:
        start = time.perf_counter()
        base_url = f"{_base_url}/v1"
        client = AsyncOpenAI(api_key=AUTH_TOKEN, base_url=base_url)

        settings = {
            "model": f"openrag-{partition}",
            "messages": [
                {
                    "role": "user",
                    "content": query,
                }
            ],
            "temperature": CONFIG.benchmark.target.temperature,
            "stream": False,
            "logprobs": CONFIG.benchmark.target.logprobs,
            "timeout": CONFIG.benchmark.target.timeout,
        }

        try:
            res = await client.chat.completions.create(**settings)

            latency = time.perf_counter() - start
            response_llm = res.choices[0].message.content
            sources = _extract_sources(res)
            # Normalize chunk IDs to str so retrieval metrics still match if OpenRAG
            # ever serializes _id as a string (Milvus int64 IDs exceed JS safe-int
            # range, a common reason to stringify). Reference IDs are str-normalized
            # to match — otherwise int-vs-str set ops silently zero every metric.
            list_source_chunk_ids = [str(item["_id"]) for item in sources if "_id" in item]
            list_chunk_urls = [item.get("chunk_url") for item in sources]

            mean_logprob, perplexity, tokens, token_logprobs = _extract_logprob_metrics(res)

            return (
                response_llm,
                list_source_chunk_ids,
                list_chunk_urls,
                latency,
                mean_logprob,
                perplexity,
                tokens,
                token_logprobs,
            )
        except (
            AuthenticationError,
            PermissionDeniedError,
            NotFoundError,
            APIConnectionError,
        ) as e:
            # Systematic failure (bad AUTH_TOKEN, wrong partition, unreachable
            # server). Surface at WARNING so it isn't lost in debug noise, but still
            # return None so one row can't abort the whole run — the all-failed
            # guard in main() turns a run-wide failure into a hard error.
            latency = time.perf_counter() - start
            logger.warning(
                f"OpenRAG query failed ({type(e).__name__}) for model 'openrag-{partition}' — "
                "check the partition name, AUTH_TOKEN, and that the server is reachable."
            )
            return None, [], [], latency, float("nan"), float("nan"), [], []
        except Exception as e:
            latency = time.perf_counter() - start
            logger.debug(f"Error fetching chunks and response: {e}")
            return None, [], [], latency, float("nan"), float("nan"), [], []


def _extract_sources(res) -> list[dict]:
    """Pull the source list out of a ChatCompletion's non-standard ``extra`` field.

    OpenRAG returns the cited sources as a JSON string in ``res.extra``
    (``{"sources": [...]}``). This is not part of the OpenAI schema, so guard every
    step: a missing attribute, a non-JSON payload, or an unexpected shape all degrade
    to an empty list rather than raising and dropping the whole row."""
    raw = getattr(res, "extra", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
    except (ValueError, TypeError) as e:
        logger.debug(f"Could not parse sources from res.extra: {e}")
        return []
    return [s for s in sources if isinstance(s, dict)]


def _extract_logprob_metrics(res) -> tuple[float, float, list[str], list[float]]:
    """Pull per-token logprobs from a ChatCompletion.

    Returns (mean_logprob, perplexity, tokens, token_logprobs). The latter two are
    parallel arrays in generation order, used downstream for per-question logprob
    visualisations. Returns (nan, nan, [], []) if logprobs are absent."""
    try:
        token_lps = res.choices[0].logprobs.content
    except AttributeError:
        return float("nan"), float("nan"), [], []
    if not token_lps:
        return float("nan"), float("nan"), [], []
    tokens: list[str] = []
    lps: list[float] = []
    for t in token_lps:
        if t.logprob is None:
            continue
        tokens.append(t.token)
        lps.append(t.logprob)
    if not lps:
        return float("nan"), float("nan"), [], []
    mean_lp = sum(lps) / len(lps)
    return mean_lp, math.exp(-mean_lp), tokens, lps

AUTH_TOKEN = os.environ.get("AUTH_TOKEN")
# Sentinel returned by judge functions when a judge call fails; rows carrying it
# are excluded from metric aggregation (kept out of means, counted as errors).
JUDGE_ERROR = "error"
_judge_model = os.environ.get("JUDGE_MODEL") or os.environ.get("MODEL")
_judge_base_url = os.environ.get("JUDGE_BASE_URL") or os.environ.get("BASE_URL")
_judge_api_key = os.environ.get("JUDGE_API_KEY") or os.environ.get("API_KEY")
logger.info(f"Judge model: {_judge_model} @ {_judge_base_url}")

llm_judge_settings = {
    "model": _judge_model,
    "base_url": _judge_base_url,
    "api_key": _judge_api_key,
    "temperature": CONFIG.benchmark.judge.temperature,
    "max_tokens": CONFIG.benchmark.judge.max_tokens,
    "top_p": CONFIG.benchmark.judge.top_p,
    "frequency_penalty": CONFIG.benchmark.judge.frequency_penalty,
    "presence_penalty": CONFIG.benchmark.judge.presence_penalty,
}


def _lenient_structured_judge(schema):
    """``with_structured_output`` with the JSON schema as a dict, not the pydantic class.

    The request payload (``response_format`` json_schema guidance) is identical,
    but with a pydantic class langchain routes the call through the OpenAI SDK's
    ``.parse()`` API, which strict-parses the content *inside* ``ainvoke`` —
    before the ``include_raw`` fallback — so judge output with raw control
    characters (a known vLLM guided-decoding quirk) raises instead of landing in
    ``parsing_error``. With a dict schema the SDK skips validation, lenient
    parsing/repair happens in ``_ainvoke_with_retry`` / ``_parse_judge_json``.
    """
    return ChatOpenAI(**llm_judge_settings).with_structured_output(
        {"name": schema.__name__, "schema": schema.model_json_schema()},
        include_raw=True,
    )


llm_completion_judge = _lenient_structured_judge(CompletionEvaluationResponse)
llm_precision_judge = _lenient_structured_judge(PrecisionEvaluationResponse)
llm_completion_judge_cot = _lenient_structured_judge(CompletionEvaluationCoTResponse)
llm_precision_judge_cot = _lenient_structured_judge(PrecisionEvaluationCoTResponse)
llm_completion_judge_cot_en = _lenient_structured_judge(CompletionEvaluationCoTResponseEN)
llm_precision_judge_cot_en = _lenient_structured_judge(PrecisionEvaluationCoTResponseEN)
llm_faithfulness_judge = _lenient_structured_judge(FaithfulnessEvaluationResponse)
llm_refusal_judge = _lenient_structured_judge(RefusalEvaluationResponse)
llm_label_judge = _lenient_structured_judge(ResponseLabelResponse)
llm_answer_relevancy_judge = _lenient_structured_judge(AnswerRelevancyEvaluationResponse)
llm_context_relevancy_judge = _lenient_structured_judge(ContextRelevancyEvaluationResponse)


def _escape_json_string_control_chars(s: str) -> str:
    out = []
    in_string = False
    escaped = False
    for ch in s:
        if escaped:
            if ord(ch) < 0x20:
                # Mangled escape like "\<newline>" — the backslash is already
                # emitted, so complete it into a legal escape sequence.
                if ch == "\n":
                    out.append("n")
                elif ch == "\r":
                    out.append("r")
                elif ch == "\t":
                    out.append("t")
                else:
                    out.append(f"u{ord(ch):04x}")
            else:
                out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ord(ch) < 0x20:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(f"\\u{ord(ch):04x}")
            continue
        if not in_string and ord(ch) < 0x20:
            continue
        out.append(ch)
    return "".join(out)


def _drop_incomplete_tail(obj):
    """Drop trailing dict items from list fields when they lack a key their peers
    have — the signature of a max_tokens-truncated judge response (e.g. a final
    statement emitted without its verdict)."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                full_keys = max((set(x.keys()) for x in val), key=len)
                while val and set(val[-1].keys()) < full_keys:
                    val.pop()
                obj[key] = val
    return obj


def _parse_judge_json(raw: str, schema):
    """Parse possibly-dirty judge JSON into ``schema``.

    Judge models frequently emit JSON that is illegal under strict parsers:
    wrapped in markdown ``` fences, or with raw control characters (newlines,
    tabs, \\u0000-\\u001F) inside string values. Pydantic's
    ``model_validate_json`` has no lenient mode and rejects these outright, so
    we parse with the stdlib ``json.loads(strict=False)`` (which *permits*
    control characters inside strings) and validate the resulting object. Only
    if that still fails do we fall back to the manual control-char escaper.
    """
    text = raw.strip()
    # Strip ```json ... ``` (or bare ``` ... ```) fences if the model added them.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return schema.model_validate(json.loads(text, strict=False))
    except (json.JSONDecodeError, ValueError):
        pass
    # Harder breakage (e.g. a stray backslash before a control char): repair
    # escapes/control chars, then lenient-parse again.
    repaired = _escape_json_string_control_chars(text)
    try:
        return schema.model_validate(json.loads(repaired, strict=False))
    except (json.JSONDecodeError, ValueError):
        # Truncated output (hit max_tokens): complete missing closers and parse.
        obj = parse_partial_json(repaired)
        try:
            return schema.model_validate(obj)
        except ValueError:
            # Trailing list item truncated mid-object (e.g. a statement with no
            # verdict yet) — drop incomplete tail items, then validate.
            return schema.model_validate(_drop_incomplete_tail(obj))


async def _ainvoke_with_retry(judge, schema, messages, attempts: int = CONFIG.benchmark.judge.retry_attempts):
    last_exc = None
    for attempt in range(attempts):
        try:
            result = await judge.ainvoke(messages)
            parsed = result.get("parsed")
            if isinstance(parsed, schema):
                return parsed
            if parsed is not None:
                # Dict-schema judges return a plain dict — validate it here.
                return schema.model_validate(parsed)
            raw = result["raw"].content if result.get("raw") is not None else ""
            return _parse_judge_json(raw, schema)
        except LengthFinishReasonError as e:
            # Output hit max_tokens (often a guided-decoding whitespace loop
            # after the JSON itself) — salvage the truncated content rather
            # than burning a retry on the same limit.
            last_exc = e
            content = e.completion.choices[0].message.content or ""
            try:
                return _parse_judge_json(content, schema)
            except (json.JSONDecodeError, ValueError) as salvage_exc:
                last_exc = salvage_exc
                if attempt < attempts - 1:
                    logger.debug(
                        f"Judge hit max_tokens and salvage failed, retry {attempt + 1}/{attempts - 1}: {salvage_exc}"
                    )
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                logger.debug(f"Judge parse retry {attempt + 1}/{attempts - 1}: {e}")
    raise last_exc


async def is_refusal(query: str, generated_answer: str, semaphore: asyncio.Semaphore) -> bool | None:
    """Returns True if the answer is a refusal/abstention, False if it attempts to answer, None on error."""
    if not generated_answer or not generated_answer.strip():
        return True
    async with semaphore:
        try:
            response = await _ainvoke_with_retry(
                llm_refusal_judge,
                RefusalEvaluationResponse,
                [
                    {"role": "system", "content": REFUSAL_JUDGE_PROMPT_EN},
                    {"role": "user", "content": f"query: {query}\ngenerated_answer: {generated_answer}"},
                ],
            )
            return response.verdict == "refusal"
        except Exception as e:
            logger.debug(f"Error evaluating refusal: {e}")
            return None

async def fetch_chunk_texts(
    chunk_urls: list[str],
    auth_token: str,
    semaphore: asyncio.Semaphore,
) -> list[str]:
    """Fetch chunk page_content for each chunk_url. Returns empty string for chunks that fail."""
    if not chunk_urls:
        return []
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    async def _fetch(client: httpx.AsyncClient, url: str | None) -> str:
        if not url:
            return ""
        async with semaphore:
            try:
                r = await client.get(url)
                r.raise_for_status()
                return r.json().get("page_content", "")
            except Exception as e:
                logger.debug(f"Error fetching chunk {url}: {e}")
                return ""

    # One client for the whole batch: the chunk URLs share a host, so connection
    # pooling / keep-alive saves a TCP+TLS handshake on every fetch after the first.
    async with httpx.AsyncClient(headers=headers, timeout=CONFIG.benchmark.chunk_fetch_timeout) as client:
        return await asyncio.gather(*[_fetch(client, u) for u in chunk_urls])


def _format_context(chunks: list[str]) -> str:
    """Join non-empty chunks into numbered '[Source N]' blocks separated by '---'."""
    return "\n\n---\n\n".join(
        f"[Source {i + 1}]\n{c}" for i, c in enumerate(chunks) if c
    )


async def faithfulness_judgment_per_question(
    query: str,
    generated_answer: str,
    context_chunks: list[str],
    semaphore: asyncio.Semaphore,
) -> tuple[float, int, int]:
    """Returns (faithfulness_score, supported_claims, total_claims).

    Score = supported / total. Returns 1.0 if no factual claims (e.g. refusal)."""
    if not generated_answer or not context_chunks:
        return float("nan"), 0, 0

    context_text = _format_context(context_chunks)
    if not context_text:
        return float("nan"), 0, 0

    user_msg = f"""query: {query}

context:
{context_text}

generated_answer: {generated_answer}
"""
    async with semaphore:
        try:
            response = await _ainvoke_with_retry(
                llm_faithfulness_judge,
                FaithfulnessEvaluationResponse,
                [
                    {"role": "system", "content": FAITHFULNESS_JUDGE_PROMPT_EN},
                    {"role": "user", "content": user_msg},
                ],
            )
            total = len(response.claims)
            if total == 0:
                return 1.0, 0, 0
            supported = sum(1 for c in response.claims if c.verdict == "supported")
            return supported / total, supported, total
        except Exception as e:
            logger.debug(f"Error evaluating faithfulness: {e}")
            return float("nan"), 0, 0


async def answer_relevancy_judgment_per_question(
    query: str,
    generated_answer: str,
    semaphore: asyncio.Semaphore,
) -> tuple[float, int, int]:
    """Reference-free answer relevancy. Returns (score, n_relevant, n_total).

    Score = relevant statements / total statements extracted from the answer.
    Needs only the query + answer (no ground truth, no context). Returns
    (nan, 0, 0) for refusals / empty answers (no statements to score)."""
    if not generated_answer or not generated_answer.strip():
        return float("nan"), 0, 0

    user_msg = f"query: {query}\n\ngenerated_answer: {generated_answer}\n"
    async with semaphore:
        try:
            response = await _ainvoke_with_retry(
                llm_answer_relevancy_judge,
                AnswerRelevancyEvaluationResponse,
                [
                    {"role": "system", "content": ANSWER_RELEVANCY_JUDGE_PROMPT_EN},
                    {"role": "user", "content": user_msg},
                ],
            )
            total = len(response.statements)
            if total == 0:
                return float("nan"), 0, 0
            relevant = sum(1 for s in response.statements if s.verdict == "relevant")
            return relevant / total, relevant, total
        except Exception as e:
            logger.debug(f"Error evaluating answer relevancy: {e}")
            return float("nan"), 0, 0


async def context_relevancy_judgment_per_question(
    query: str,
    context_chunks: list[str],
    semaphore: asyncio.Semaphore,
) -> tuple[float, int, int]:
    """Reference-free context (retriever) relevancy. Returns (score, n_relevant, n_total).

    Score = relevant statements / total statements extracted from the retrieved
    context. Needs only the query + context (no ground-truth chunk IDs), so it
    complements the ID-based retrieval metrics. Single LLM call over the joined
    context (matching the faithfulness judge), not one call per chunk. Returns
    (nan, 0, 0) when there is no context."""
    if not context_chunks:
        return float("nan"), 0, 0

    context_text = _format_context(context_chunks)
    if not context_text:
        return float("nan"), 0, 0

    user_msg = f"query: {query}\n\ncontext:\n{context_text}\n"
    async with semaphore:
        try:
            response = await _ainvoke_with_retry(
                llm_context_relevancy_judge,
                ContextRelevancyEvaluationResponse,
                [
                    {"role": "system", "content": CONTEXT_RELEVANCY_JUDGE_PROMPT_EN},
                    {"role": "user", "content": user_msg},
                ],
            )
            total = len(response.statements)
            if total == 0:
                return float("nan"), 0, 0
            relevant = sum(1 for s in response.statements if s.verdict == "relevant")
            return relevant / total, relevant, total
        except Exception as e:
            logger.debug(f"Error evaluating context relevancy: {e}")
            return float("nan"), 0, 0


async def response_judgment_per_question(
    query: str,
    llm_answer: str,
    openrag_answer: str,
    semaphore: asyncio.Semaphore,
):
    # Empty/whitespace answer: deterministic floor (covers nothing, aligns with
    # nothing) instead of spending two judge calls scoring "". Returns int scores
    # (not the "error" sentinel) so the row is kept, not dropped as a judge failure.
    if not openrag_answer or not openrag_answer.strip():
        return 1, 1
    s = f"""Here are the details needed to evaluate a language model's (LLM) answer to a question:
    query: {query}
    true_answer: {llm_answer}
    generated_answer: {openrag_answer}
    """

    async def _complete():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_completion_judge,
                CompletionEvaluationResponse,
                [{"role": "system", "content": COMPLETION_JUDGE_PROMPT_EN}, {"role": "user", "content": s}],
            )

    async def _precise():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_precision_judge,
                PrecisionEvaluationResponse,
                [{"role": "system", "content": PRECISION_JUDGE_PROMPT_EN}, {"role": "user", "content": s}],
            )

    try:
        response_for_completion, response_for_precision = await asyncio.gather(_complete(), _precise())
        return response_for_completion.score, response_for_precision.score
    except Exception as e:
        logger.debug(f"Error evaluating response: {e}")
        return JUDGE_ERROR, JUDGE_ERROR


async def evaluate_answerable_row(
    query: str,
    llm_answer: str,
    openrag_response: str,
    judge_semaphore: asyncio.Semaphore,
):
    """Run completion, precision, refusal, and answer-relevancy judges concurrently for a single row.

    Returns (comp_prec_tuple, refusal_bool, answer_relevancy_tuple). Faithfulness and
    context relevancy are judged separately because they require async chunk fetching
    and tend to dominate per-row latency. Answer relevancy is reference-free (query +
    answer only), so it is cheap and runs on every answerable row here.
    """
    comp_prec, refusal, answer_rel = await asyncio.gather(
        response_judgment_per_question(query, llm_answer, openrag_response, judge_semaphore),
        is_refusal(query, openrag_response, judge_semaphore),
        answer_relevancy_judgment_per_question(query, openrag_response, judge_semaphore),
    )
    return comp_prec, refusal, answer_rel


async def response_judgment_cot_per_question(
    query: str,
    llm_answer: str,
    openrag_answer: str,
    semaphore: asyncio.Semaphore,
):
    """Chain-of-thought variant: returns (completion_score, precision_score, completion_reasoning, precision_reasoning)."""
    s = f"""Voici les détails nécessaires pour évaluer la réponse d'un modèle de langage (LLM) à une question :
    query: {query}
    true_answer: {llm_answer}
    generated_answer: {openrag_answer}
    """

    async def _complete():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_completion_judge_cot,
                CompletionEvaluationCoTResponse,
                [{"role": "system", "content": COMPLETION_JUDGE_PROMPT_COT}, {"role": "user", "content": s}],
            )

    async def _precise():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_precision_judge_cot,
                PrecisionEvaluationCoTResponse,
                [{"role": "system", "content": PRECISION_JUDGE_PROMPT_COT}, {"role": "user", "content": s}],
            )

    try:
        comp, prec = await asyncio.gather(_complete(), _precise())
        return comp.score, prec.score, comp.reasoning, prec.reasoning
    except Exception as e:
        logger.debug(f"Error evaluating response (CoT): {e}")
        return JUDGE_ERROR, JUDGE_ERROR, "", ""


async def response_judgment_cot_en_per_question(
    query: str,
    llm_answer: str,
    openrag_answer: str,
    semaphore: asyncio.Semaphore,
):
    """English chain-of-thought variant: returns (completion_score, precision_score, completion_reasoning, precision_reasoning)."""
    s = f"""Here are the details needed to evaluate a language model's (LLM) answer to a question:
    query: {query}
    true_answer: {llm_answer}
    generated_answer: {openrag_answer}
    """

    async def _complete():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_completion_judge_cot_en,
                CompletionEvaluationCoTResponseEN,
                [{"role": "system", "content": COMPLETION_JUDGE_PROMPT_COT_EN}, {"role": "user", "content": s}],
            )

    async def _precise():
        async with semaphore:
            return await _ainvoke_with_retry(
                llm_precision_judge_cot_en,
                PrecisionEvaluationCoTResponseEN,
                [{"role": "system", "content": PRECISION_JUDGE_PROMPT_COT_EN}, {"role": "user", "content": s}],
            )

    try:
        comp, prec = await asyncio.gather(_complete(), _precise())
        return comp.score, prec.score, comp.reasoning, prec.reasoning
    except Exception as e:
        logger.debug(f"Error evaluating response (CoT EN): {e}")
        return JUDGE_ERROR, JUDGE_ERROR, "", ""


async def label_response_per_question(
    query: str,
    llm_answer: str,
    openrag_answer: str,
    semaphore: asyncio.Semaphore,
    language: Literal["fr", "en"] = "en",
):
    """Classify a response as 'Fully Correct', 'Incomplete', or 'Contradictory'. Returns (label, reasoning)."""
    # Empty/whitespace answer: an answered-nothing row is Incomplete by definition;
    # return a valid label (not "error") so it's counted, and skip the judge call.
    if not openrag_answer or not openrag_answer.strip():
        return "Incomplete", "Empty answer — no content to evaluate."
    if language == "en":
        system_prompt = LABEL_JUDGE_PROMPT_EN
        user_msg = f"""Here are the details needed to label a language model's (LLM) answer to a question:
    query: {query}
    true_answer: {llm_answer}
    generated_answer: {openrag_answer}
    """
    else:
        system_prompt = LABEL_JUDGE_PROMPT
        user_msg = f"""Voici les détails nécessaires pour classer la réponse d'un modèle de langage (LLM) à une question :
    query: {query}
    true_answer: {llm_answer}
    generated_answer: {openrag_answer}
    """
    async with semaphore:
        try:
            r = await _ainvoke_with_retry(
                llm_label_judge,
                ResponseLabelResponse,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
            return r.label, r.reasoning
        except Exception as e:
            logger.debug(f"Error labeling response: {e}")
            return JUDGE_ERROR, ""

LABEL_LANGUAGE: Literal["fr", "en"] = CONFIG.benchmark.label_language

COT_AUDIT_FRACTION = CONFIG.benchmark.cot_audit_fraction
COT_AUDIT_SEED = CONFIG.benchmark.cot_audit_seed

# Faithfulness is the slowest judge (per-chunk fetch + claim extraction), so score
# only a random sample of judged rows. Set faithfulness_fraction = 1.0 for full coverage.
FAITHFULNESS_FRACTION = CONFIG.benchmark.faithfulness_fraction
FAITHFULNESS_SEED = CONFIG.benchmark.faithfulness_seed


class Element(TypedDict):
    question: str
    llm_answer: str
    chunks: list[dict]


def _load_and_validate_dataset(path: str) -> list[Element]:
    """Load a (golden) dataset JSON file and validate its schema.

    Required per-row: ``question`` (non-empty str), ``llm_answer`` (str).
    Optional per-row: ``answerable`` (bool, default True), ``chunks`` (list of
    objects each with an ``id`` field, default []).
    Rows without ``chunks`` are still scored on generation/judges; retrieval
    metrics are skipped for those rows.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Dataset {path!r} must be a JSON list of entries.")

    normalized: list[Element] = []
    n_with_chunks = 0
    n_answerable = 0
    n_unanswerable = 0
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Dataset entry #{i} is not an object.")
        question = row.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Dataset entry #{i} is missing a non-empty 'question' string.")
        llm_answer = row.get("llm_answer")
        if not isinstance(llm_answer, str):
            raise ValueError(f"Dataset entry #{i} is missing a 'llm_answer' string.")
        answerable = bool(row.get("answerable", True))
        chunks = row.get("chunks") or []
        if not isinstance(chunks, list):
            raise ValueError(f"Dataset entry #{i} 'chunks' must be a list.")
        for j, c in enumerate(chunks):
            if not isinstance(c, dict) or "id" not in c:
                raise ValueError(
                    f"Dataset entry #{i} chunk #{j} must be an object with an 'id' field."
                )
        if chunks:
            n_with_chunks += 1
        if answerable:
            n_answerable += 1
        else:
            n_unanswerable += 1
        normalized.append({**row, "answerable": answerable, "chunks": chunks})

    logger.info(
        f"Loaded {len(normalized)} entries from {path} "
        f"(answerable={n_answerable}, unanswerable={n_unanswerable}, "
        f"with_ground_truth_chunks={n_with_chunks})"
    )
    if n_with_chunks == 0:
        logger.warning(
            "No rows contain ground-truth chunks — retrieval metrics will be skipped."
        )
    return normalized


def _score_breakdown(eval_results, by):
    """Mean answerable-row scores grouped by a metadata column.

    ``by`` is "question_type" or "difficulty". Returns
    {category: {n, completion_mean, precision_mean, answer_relevancy_mean, ndcg_mean}}.
    Safe on empty frames and on legacy (v1) datasets (a single "unknown" bucket).
    """
    if by not in eval_results.columns or eval_results.empty:
        return {}
    out: dict = {}
    for key, grp in eval_results.groupby(by):
        ar = grp["answer_relevancy"].dropna() if "answer_relevancy" in grp else pd.Series(dtype=float)
        ndcg = grp["nDCG"].dropna() if "nDCG" in grp else pd.Series(dtype=float)
        out[str(key)] = {
            "n": int(len(grp)),
            "completion_mean": round(float(grp["completion_evaluation"].mean()), 3) if len(grp) else None,
            "precision_mean": round(float(grp["precision_evaluation"].mean()), 3) if len(grp) else None,
            "answer_relevancy_mean": round(float(ar.mean()), 3) if not ar.empty else None,
            "ndcg_mean": round(float(ndcg.mean()), 3) if not ndcg.empty else None,
        }
    return out


async def run_single_query(
    query: str,
    partition: str,
    base_url: str,
    reference_answer: str | None = None,
    reference_chunk_ids: list[str] | None = None,
) -> dict:
    """Run the benchmark judges for a single ad-hoc query, writing nothing to disk.

    Sends one query to OpenRAG, then scores it with the reference-free judges
    (answer relevancy, context relevancy, faithfulness, refusal) plus latency and
    logprob signals. If ``reference_answer`` is given, also runs the completion /
    precision / label judges and the n-gram generation metrics. If
    ``reference_chunk_ids`` is given, also computes the ID-based retrieval metrics.

    Returns a plain dict for display. Used by the dashboard's single-query mode;
    intentionally does not persist any report artifact.
    """
    auth_token = AUTH_TOKEN
    # One query => trivial concurrency; a single shared semaphore is enough.
    sem = asyncio.Semaphore(CONFIG.benchmark.judge_concurrency)

    (
        response,
        chunk_ids,
        chunk_urls,
        latency,
        mean_logprob,
        perplexity,
        _tokens,
        _token_logprobs,
    ) = await retrieve_response_and_docs_openrag(
        query=query, partition=partition, _base_url=base_url, semaphore=sem
    )

    def _nan_to_none(x):
        return None if (x is None or (isinstance(x, float) and math.isnan(x))) else x

    result: dict = {
        "query": query,
        "response": response,
        "chunk_ids": chunk_ids,
        "chunk_urls": chunk_urls,
        "latency": _nan_to_none(latency),
        "mean_logprob": _nan_to_none(mean_logprob),
        "perplexity": _nan_to_none(perplexity),
    }

    if response is None:
        result["error"] = "OpenRAG returned no response (check the server / base URL / partition)."
        return result

    # Fetch retrieved chunk texts once; faithfulness + context relevancy share them.
    chunk_texts = await fetch_chunk_texts(chunk_urls, auth_token, sem)

    refusal, answer_rel, faith, ctx_rel = await asyncio.gather(
        is_refusal(query, response, sem),
        answer_relevancy_judgment_per_question(query, response, sem),
        faithfulness_judgment_per_question(query, response, chunk_texts, sem),
        context_relevancy_judgment_per_question(query, chunk_texts, sem),
    )

    def _ratio(t):
        score, relevant, total = t
        return {
            "score": _nan_to_none(score),
            "relevant": int(relevant),
            "total": int(total),
        }

    result["refusal"] = refusal
    result["answer_relevancy"] = _ratio(answer_rel)
    result["faithfulness"] = _ratio(faith)
    result["context_relevancy"] = _ratio(ctx_rel)
    result["chunk_texts"] = chunk_texts

    if reference_answer:
        (completion, precision), label = await asyncio.gather(
            response_judgment_per_question(query, reference_answer, response, sem),
            label_response_per_question(query, reference_answer, response, sem, LABEL_LANGUAGE),
        )
        result["completion"] = completion
        result["precision"] = precision
        result["label"] = label[0]
        result["label_reasoning"] = label[1]
        result["generation"] = compute_generation_metrics(reference_answer, response)

    if reference_chunk_ids:
        # str-normalize to match the str-normalized retrieved IDs (see _id extraction).
        reference_chunk_ids = [str(c) for c in reference_chunk_ids]
        result["retrieval"] = {
            "hit_rate": compute_hit_at_k(reference_chunk_ids, chunk_ids, k=5),
            "mrr": compute_mrr(reference_chunk_ids, chunk_ids),
            "recall": len(set(reference_chunk_ids) & set(chunk_ids)) / len(reference_chunk_ids),
            "precision_at_5": precision_at_k(reference_chunk_ids, chunk_ids, k=5),
            "precision_at_10": precision_at_k(reference_chunk_ids, chunk_ids, k=10),
            "map": average_precision(reference_chunk_ids, chunk_ids),
            "ndcg_at_5": ndcg_at_k(reference_chunk_ids, chunk_ids, 5),
            "ndcg_at_10": ndcg_at_k(reference_chunk_ids, chunk_ids, 10),
        }

    return result


async def main(
    partition: str = CONFIG.common.partition,
    base_url: str = os.environ.get("API_BASE_URL"),
    dataset_path: str = CONFIG.common.dataset_path,
    output_dir: str = CONFIG.common.output_dir,
    limit: int | None = CONFIG.benchmark.limit,
    run_id: str | None = None,
    version: str | None = None,
    git_commit: str | None = None,
    no_retrieval: bool = False,
):
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs(output_dir, exist_ok=True)

    eval_dataset = _load_and_validate_dataset(dataset_path)

    list_response_answer_reference = eval_dataset if limit is None else eval_dataset[:limit]

    openrag_api_base_url = base_url

    # Create shared semaphores for rate limiting
    openrag_semaphore = asyncio.Semaphore(CONFIG.benchmark.openrag_concurrency)  # Limit concurrent OpenRAG requests
    judge_semaphore = asyncio.Semaphore(CONFIG.benchmark.judge_concurrency)
    chunk_fetch_semaphore = asyncio.Semaphore(CONFIG.benchmark.chunk_fetch_concurrency)  # Limit concurrent chunk fetches
    auth_token = AUTH_TOKEN

    # Create tasks for OpenRAG API calls
    tasks = [
        retrieve_response_and_docs_openrag(
            query=resp_ans_reference["question"],
            partition=partition,
            _base_url=openrag_api_base_url,
            semaphore=openrag_semaphore,
        )
        for resp_ans_reference in list_response_answer_reference
    ]

    openrag_answer_chunk_ids_l = await tqdm.gather(*tasks, desc="Fetching")

    # If not a single OpenRAG query returned a response, there is nothing to score —
    # almost always a systematic problem (wrong partition, bad AUTH_TOKEN, or the
    # server being down) rather than per-row flakiness. Fail loudly instead of
    # writing an empty, misleading report. (r[0] is the response; None == failed.)
    n_ok = sum(1 for r in openrag_answer_chunk_ids_l if r[0] is not None)
    if n_ok == 0:
        raise RuntimeError(
            f"All {len(openrag_answer_chunk_ids_l)} OpenRAG queries failed (0 responses) for "
            f"model 'openrag-{partition}'. Check the partition name, AUTH_TOKEN, and that the "
            f"server at {openrag_api_base_url} is reachable — see the warnings above."
        )

    hit_rates, MRRs, recalls = [], [], []
    row_judge_tasks = []
    precision_at_5_list = []
    precision_at_10_list = []
    map_scores = []
    r_precision_scores = []
    recall_at_5_list = []
    recall_at_10_list = []
    ndcg_at_5_list = []
    ndcg_at_10_list = []

    latencies = []
    mean_logprobs = []
    perplexities = []
    token_sequences: list[list[str]] = []
    token_logprob_sequences: list[list[float]] = []
    rouge1_list, rouge2_list, rougeL_list, bleu_list, meteor_list = [], [], [], [], []

    unanswerable_refusal_tasks = []
    unanswerable_total = 0
    unanswerable_no_response = 0
    # Aligned 1:1 with unanswerable_refusal_tasks: the seed (topic) chunk IDs the
    # adversarial question was generated from, and the chunk IDs the retriever
    # actually returned. Used for the topic-conditioned abstention metric below.
    unanswerable_meta: list[dict] = []
    # Inputs aligned 1:1 with row_judge_tasks (answerable & non-None response only)
    judged_inputs = []
    judged_responses = []
    judged_chunk_urls = []
    # Per-judged-row tracking (aligned 1:1 with judged_inputs).
    # NaN / 0 markers used for rows whose golden entry has no ground-truth chunks.
    ndcg_per_judged_row: list[float] = []
    n_chunks_per_judged_row: list[int] = []


    for idx, ((openrag_response, openrag_chunk_ids, openrag_chunk_urls, latency, mean_lp, ppl, tokens, token_lps), input_reference) in enumerate(
        zip(openrag_answer_chunk_ids_l, list_response_answer_reference)
    ):
        latencies.append(latency)
        is_answerable = input_reference.get("answerable", True)

        # Unanswerable slice: only score abstention; skip retrieval/generation/faithfulness
        if not is_answerable:
            unanswerable_total += 1
            if openrag_response is None:
                unanswerable_no_response += 1
                continue
            unanswerable_refusal_tasks.append(
                is_refusal(input_reference["question"], openrag_response, judge_semaphore)
            )
            unanswerable_meta.append(
                {
                    "topic_chunks": [str(c) for c in (input_reference.get("topic_chunks") or [])],
                    "retrieved_ids": openrag_chunk_ids,
                }
            )
            continue

        if openrag_response is None:
            continue

        # Per-answerable-question logprob signals; aligned 1:1 with judged_inputs/judged_responses.
        mean_logprobs.append(mean_lp)
        perplexities.append(ppl)
        token_sequences.append(tokens)
        token_logprob_sequences.append(token_lps)

        # Retrieval metrics require ground-truth chunk IDs in the golden entry.
        # When absent (Q+A-only datasets), skip them but still run generation/judges.
        # --no-retrieval forces the skip even when chunks ARE present — useful when the
        # dataset's chunk IDs are from a different index (e.g. a fixed dataset scored
        # against a freshly re-indexed instance) so ID-based retrieval would be noise.
        gt_chunks = [] if no_retrieval else (input_reference.get("chunks") or [])
        if gt_chunks:
            chunk_id_reference = [str(c["id"]) for c in gt_chunks]
            hit_rates.append(compute_hit_at_k(chunk_id_reference, openrag_chunk_ids, k=5))
            MRRs.append(compute_mrr(chunk_id_reference, openrag_chunk_ids))

            precision_at_5_list.append(precision_at_k(chunk_id_reference, openrag_chunk_ids, k=5))
            precision_at_10_list.append(precision_at_k(chunk_id_reference, openrag_chunk_ids, k=10))

            map_scores.append(average_precision(chunk_id_reference, openrag_chunk_ids))
            r_precision_scores.append(r_precision(chunk_id_reference, openrag_chunk_ids))

            recall_at_5_list.append(recall_at_k(chunk_id_reference, openrag_chunk_ids, 5))
            recall_at_10_list.append(recall_at_k(chunk_id_reference, openrag_chunk_ids, 10))

            ndcg_at_5_list.append(ndcg_at_k(chunk_id_reference, openrag_chunk_ids, 5))
            ndcg_at_10_list.append(ndcg_at_k(chunk_id_reference, openrag_chunk_ids, 10))

            recall = len(set(chunk_id_reference) & set(openrag_chunk_ids)) / len(chunk_id_reference)
            recalls.append(recall)

            # Full-list nDCG over every retrieved chunk — same metric family as the
            # nDCG@5/@10 above, just with k = the full retrieved-list length.
            nDCG_score = ndcg_at_k(
                chunk_id_reference, openrag_chunk_ids, k=len(openrag_chunk_ids)
            )
            ndcg_per_judged_row.append(nDCG_score)
            n_chunks_per_judged_row.append(len(gt_chunks))
        else:
            ndcg_per_judged_row.append(float("nan"))
            n_chunks_per_judged_row.append(0)

        gen_metrics = compute_generation_metrics(input_reference["llm_answer"], openrag_response)
        rouge1_list.append(gen_metrics["rouge1"])
        rouge2_list.append(gen_metrics["rouge2"])
        rougeL_list.append(gen_metrics["rougeL"])
        bleu_list.append(gen_metrics["bleu"])
        meteor_list.append(gen_metrics["meteor"])

        row_judge_tasks.append(
            evaluate_answerable_row(
                query=input_reference["question"],
                llm_answer=input_reference["llm_answer"],
                openrag_response=openrag_response,
                judge_semaphore=judge_semaphore,
            )
        )

        judged_inputs.append(input_reference)
        judged_responses.append(openrag_response)
        judged_chunk_urls.append(openrag_chunk_urls)

    label_tasks = [
        label_response_per_question(
            query=judged_inputs[i]["question"],
            llm_answer=judged_inputs[i]["llm_answer"],
            openrag_answer=judged_responses[i],
            semaphore=judge_semaphore,
            language=LABEL_LANGUAGE,
        )
        for i in range(len(judged_inputs))
    ]

    # Faithfulness on a random sample only — it's the slowest judge. Context
    # relevancy (reference-free retriever signal) reuses the same fetched chunk
    # texts, so it rides on the same sample for free rather than re-fetching.
    async def _faithfulness_for(question, answer, chunk_urls):
        chunk_texts = await fetch_chunk_texts(chunk_urls, auth_token, chunk_fetch_semaphore)
        faith, ctx_rel = await asyncio.gather(
            faithfulness_judgment_per_question(question, answer, chunk_texts, judge_semaphore),
            context_relevancy_judgment_per_question(question, chunk_texts, judge_semaphore),
        )
        return faith, ctx_rel

    rng_faith = random.Random(FAITHFULNESS_SEED)
    n_faith = max(1, int(len(judged_inputs) * FAITHFULNESS_FRACTION)) if judged_inputs else 0
    faith_indices = (
        sorted(rng_faith.sample(range(len(judged_inputs)), min(n_faith, len(judged_inputs))))
        if n_faith
        else []
    )
    faithfulness_tasks = [
        _faithfulness_for(judged_inputs[i]["question"], judged_responses[i], judged_chunk_urls[i])
        for i in faith_indices
    ]

    rng_cot = random.Random(COT_AUDIT_SEED)
    n_cot = max(1, int(len(judged_inputs) * COT_AUDIT_FRACTION)) if judged_inputs else 0
    audit_indices = rng_cot.sample(range(len(judged_inputs)), min(n_cot, len(judged_inputs))) if n_cot else []
    cot_tasks = [
        response_judgment_cot_en_per_question(
            query=judged_inputs[i]["question"],
            llm_answer=judged_inputs[i]["llm_answer"],
            openrag_answer=judged_responses[i],
            semaphore=judge_semaphore,
        )
        for i in audit_indices
    ]

    (
        row_results,
        faithfulness_results,
        unanswerable_refusals,
        label_results,
        cot_results,
    ) = await asyncio.gather(
        tqdm.gather(*row_judge_tasks, desc="Evaluating responses (completion+precision+refusal/row)"),
        tqdm.gather(*faithfulness_tasks, desc=f"Evaluating faithfulness ({len(faith_indices)} samples)")
        if faithfulness_tasks
        else asyncio.gather(),
        tqdm.gather(*unanswerable_refusal_tasks, desc="Evaluating refusals (unanswerable)"),
        tqdm.gather(*label_tasks, desc=f"Labeling responses ({LABEL_LANGUAGE})") if label_tasks else asyncio.gather(),
        tqdm.gather(*cot_tasks, desc=f"CoT evaluation ({len(audit_indices)} samples)") if cot_tasks else asyncio.gather(),
    )

    # Unpack bundled per-row results into the legacy lists consumed by downstream code.
    llm_judge_scores = [(r[0][0], r[0][1]) for r in row_results]
    answerable_refusals = [r[1] for r in row_results]
    # Reference-free answer relevancy, aligned 1:1 with judged_inputs.
    answer_relevancy_results = [r[2] for r in row_results]

    # The faithfulness sample bundled (faithfulness, context_relevancy) tuples;
    # split them back out. Both are aligned 1:1 with faith_indices.
    context_relevancy_results = [r[1] for r in faithfulness_results]
    faithfulness_results = [r[0] for r in faithfulness_results]

    # ===== Aggregate every headline metric exactly once =====
    # metrics_summary is the single source of truth: the console digest below,
    # the JSON summary, and the TXT digest all render from it. No metric is
    # recomputed per output channel, and empty inputs are guarded here once
    # (returning None) instead of three times, inconsistently.
    def _safe_mean(xs):
        return float(np.mean(xs)) if len(xs) else None

    def _fmt(v, spec=".3f"):
        """Render a metric value, or 'n/a' when it is None (empty input)."""
        return "n/a" if v is None else format(v, spec)

    has_retrieval = bool(hit_rates)

    # Per-row typed metadata (gaps 4–5 schema) for score breakdowns + CSV slicing.
    # judged_inputs is aligned 1:1 with the score lists below. Falls back to
    # "unknown" for legacy (v1) datasets that have no metadata block.
    _row_meta = [(ji.get("metadata") or {}) for ji in judged_inputs]
    row_question_types = [m.get("question_type", "unknown") for m in _row_meta]
    row_difficulties = [m.get("difficulty", "unknown") for m in _row_meta]

    per_row = pd.DataFrame(
        {
            "completion_evaluation": [s[0] for s in llm_judge_scores],
            "precision_evaluation": [s[1] for s in llm_judge_scores],
            "answer_relevancy": [r[0] for r in answer_relevancy_results],
            "nDCG": ndcg_per_judged_row,
            "n_chunks": n_chunks_per_judged_row,
            "mean_logprob": mean_logprobs,
            "perplexity": perplexities,
            "question_type": row_question_types,
            "difficulty": row_difficulties,
        }
    )
    eval_results = per_row[per_row["completion_evaluation"] != JUDGE_ERROR].copy()
    eval_results["completion_evaluation"] = pd.to_numeric(eval_results["completion_evaluation"])
    eval_results["precision_evaluation"] = pd.to_numeric(eval_results["precision_evaluation"])
    eval_results = eval_results.reset_index(drop=True)

    # Score breakdowns by typed-question metadata (answerable rows only). Buckets
    # collapse to a single "unknown" entry for datasets predating the v2 schema.
    score_breakdowns = {
        "by_question_type": _score_breakdown(eval_results, "question_type"),
        "by_difficulty": _score_breakdown(eval_results, "difficulty"),
    }

    # Group only the rows that had ground-truth chunks; rows with NaN nDCG are excluded.
    ndcg_scored = eval_results.dropna(subset=["nDCG"])
    avg_ndcg_per_chunk = ndcg_scored.groupby("n_chunks")["nDCG"].mean().round(3).to_dict()

    faith_scores = [r[0] for r in faithfulness_results if not math.isnan(r[0])]
    total_supported = sum(r[1] for r in faithfulness_results)
    total_claims = sum(r[2] for r in faithfulness_results)

    # Reference-free relevancy (needs no ground truth).
    ar_scores = [r[0] for r in answer_relevancy_results if not math.isnan(r[0])]
    ar_relevant = sum(r[1] for r in answer_relevancy_results)
    ar_total = sum(r[2] for r in answer_relevancy_results)

    cr_scores = [r[0] for r in context_relevancy_results if not math.isnan(r[0])]
    cr_relevant = sum(r[1] for r in context_relevancy_results)
    cr_total = sum(r[2] for r in context_relevancy_results)

    # Pair each unanswerable refusal verdict with its retrieval metadata (dropping
    # rows where the judge errored). valid_unans keeps the bare verdicts for the
    # headline abstention rate; the paired form drives the topic-conditioned metric.
    valid_unans_paired = [
        (verdict, meta)
        for verdict, meta in zip(unanswerable_refusals, unanswerable_meta)
        if verdict is not None
    ]
    valid_unans = [v for v, _ in valid_unans_paired]

    # Topic-conditioned abstention: among unanswerable rows whose adversarial
    # question was seeded from known chunks AND where the retriever actually
    # surfaced at least one of those chunks (relevant-looking context present),
    # how often did the model still correctly abstain? — the strong abstention
    # signal (abstaining despite plausible context, not just because retrieval
    # returned nothing).
    topic_rows = [(v, m) for v, m in valid_unans_paired if m["topic_chunks"]]
    topic_retrieved_rows = [
        (v, m) for v, m in topic_rows if set(m["topic_chunks"]) & set(m["retrieved_ids"])
    ]
    topic_seed_total = len(topic_rows)
    topic_seed_retrieved = len(topic_retrieved_rows)
    topic_retrieved_abstained = sum(1 for v, _ in topic_retrieved_rows if v)
    topic_retrieval_rate = (topic_seed_retrieved / topic_seed_total) if topic_seed_total else None
    abstention_given_topic_retrieved = (
        topic_retrieved_abstained / topic_seed_retrieved if topic_seed_retrieved else None
    )

    valid_ans = [r for r in answerable_refusals if r is not None]

    lat_array = np.array(latencies)
    valid_mean_lps = [x for x in mean_logprobs if not math.isnan(x)]
    valid_ppls = [x for x in perplexities if not math.isnan(x)]

    metrics_summary = {
        "retrieval": {
            "hit_rate": _safe_mean(hit_rates),
            "mrr": _safe_mean(MRRs),
            "recall": _safe_mean(recalls),
            "precision_at_5": _safe_mean(precision_at_5_list),
            "precision_at_10": _safe_mean(precision_at_10_list),
            "recall_at_5": _safe_mean(recall_at_5_list),
            "recall_at_10": _safe_mean(recall_at_10_list),
            "ndcg_at_5": _safe_mean(ndcg_at_5_list),
            "ndcg_at_10": _safe_mean(ndcg_at_10_list),
            "map": _safe_mean(map_scores),
            "r_precision": _safe_mean(r_precision_scores),
            "ndcg_mean": float(ndcg_scored["nDCG"].mean()) if len(ndcg_scored) else None,
            "ndcg_std": float(ndcg_scored["nDCG"].std()) if len(ndcg_scored) else None,
            "ndcg_by_chunk_count": {str(k): float(v) for k, v in avg_ndcg_per_chunk.items()},
        },
        "generation": {
            "rouge1": _safe_mean(rouge1_list),
            "rouge2": _safe_mean(rouge2_list),
            "rougeL": _safe_mean(rougeL_list),
            "bleu": _safe_mean(bleu_list),
            "meteor": _safe_mean(meteor_list),
        },
        "faithfulness": {
            "mean_per_question": _safe_mean(faith_scores),
            "n_scored": len(faith_scores),
            "sample_size": len(faith_indices),
            "sample_fraction": FAITHFULNESS_FRACTION,
            "supported_claims": int(total_supported),
            "total_claims": int(total_claims),
            "micro_ratio": (total_supported / total_claims) if total_claims > 0 else None,
        },
        "answer_relevancy": {
            "mean_per_question": _safe_mean(ar_scores),
            "n_scored": len(ar_scores),
            "relevant_statements": int(ar_relevant),
            "total_statements": int(ar_total),
            "micro_ratio": (ar_relevant / ar_total) if ar_total > 0 else None,
        },
        "context_relevancy": {
            "mean_per_question": _safe_mean(cr_scores),
            "n_scored": len(cr_scores),
            "sample_size": len(faith_indices),
            "sample_fraction": FAITHFULNESS_FRACTION,
            "relevant_statements": int(cr_relevant),
            "total_statements": int(cr_total),
            "micro_ratio": (cr_relevant / cr_total) if cr_total > 0 else None,
        },
        "refusal": {
            "unanswerable_total": unanswerable_total,
            "unanswerable_no_response": unanswerable_no_response,
            "abstention_rate": (sum(valid_unans) / len(valid_unans)) if valid_unans else None,
            "abstention_count": int(sum(valid_unans)) if valid_unans else 0,
            "abstention_denom": len(valid_unans),
            "false_refusal_rate": (sum(valid_ans) / len(valid_ans)) if valid_ans else None,
            "false_refusal_count": int(sum(valid_ans)) if valid_ans else 0,
            "false_refusal_denom": len(valid_ans),
            # Topic-conditioned abstention: of unanswerable rows whose seed (topic)
            # chunk was actually retrieved, how often the model still abstained.
            "topic_seed_total": topic_seed_total,
            "topic_seed_retrieved": topic_seed_retrieved,
            "topic_retrieval_rate": topic_retrieval_rate,
            "abstention_given_topic_retrieved": abstention_given_topic_retrieved,
            "topic_retrieved_abstained_count": topic_retrieved_abstained,
        },
        "latency": {
            "mean": float(lat_array.mean()) if len(lat_array) else None,
            "p50": float(np.percentile(lat_array, 50)) if len(lat_array) else None,
            "p95": float(np.percentile(lat_array, 95)) if len(lat_array) else None,
            "p99": float(np.percentile(lat_array, 99)) if len(lat_array) else None,
        },
        "logprobs": {
            "n_scored": len(valid_mean_lps),
            "mean_logprob_mean": _safe_mean(valid_mean_lps),
            "mean_logprob_median": float(np.median(valid_mean_lps)) if valid_mean_lps else None,
            "perplexity_mean": _safe_mean(valid_ppls),
            "perplexity_median": float(np.median(valid_ppls)) if valid_ppls else None,
        },
    }
    # Short aliases for the render blocks below.
    R = metrics_summary["retrieval"]
    G = metrics_summary["generation"]
    Fa = metrics_summary["faithfulness"]
    Ar = metrics_summary["answer_relevancy"]
    Cr = metrics_summary["context_relevancy"]
    Rf = metrics_summary["refusal"]
    La = metrics_summary["latency"]
    Lp = metrics_summary["logprobs"]

    # ===== Console digest (renders from metrics_summary) =====
    if has_retrieval:
        print(f"Average Hit Rate: {round(R['hit_rate'], 3)}")
        print(f"Average MRR: {round(R['mrr'], 3)}")
        print(f"Average Recall: {round(R['recall'], 3)}")
        print(f"Precision@5: {R['precision_at_5']:.3f}")
        print(f"Precision@10: {R['precision_at_10']:.3f}")
        print(f"MAP: {R['map']:.3f}")
        print(f"R-Precision: {R['r_precision']:.3f}")
        print(f"Recall@5: {R['recall_at_5']:.3f}")
        print(f"Recall@10: {R['recall_at_10']:.3f}")
        print(f"nDCG@5: {R['ndcg_at_5']:.3f}")
        print(f"nDCG@10: {R['ndcg_at_10']:.3f}")
    else:
        print("Retrieval metrics: n/a (no ground-truth chunks in dataset)")


    print("\n--- Generation Metrics ---")
    print(f"ROUGE-1: {_fmt(G['rouge1'])}")
    print(f"ROUGE-2: {_fmt(G['rouge2'])}")
    print(f"ROUGE-L: {_fmt(G['rougeL'])}")
    print(f"BLEU:    {_fmt(G['bleu'])}")
    print(f"METEOR:  {_fmt(G['meteor'])}")

    if len(ndcg_scored):
        print(f"\nAverage nDCG per chunk count: {avg_ndcg_per_chunk}\n")
        print(f"Average nDCG: {round(R['ndcg_mean'], 3)} +/- {R['ndcg_std']:.3f}")
    else:
        print("\nAverage nDCG: n/a (no ground-truth chunks scored)")

    print(f"\n--- Faithfulness ({len(faith_indices)}/{len(judged_inputs)} sampled, {FAITHFULNESS_FRACTION:.0%}) ---")
    if faith_scores:
        print(f"Mean per-question faithfulness: {Fa['mean_per_question']:.3f}  (n={Fa['n_scored']})")
    else:
        print("Mean per-question faithfulness: n/a (no scorable answers)")
    if total_claims > 0:
        print(f"Micro faithfulness (claims): {total_supported}/{total_claims} = {Fa['micro_ratio']:.3f}")
    else:
        print("Micro faithfulness (claims): n/a (no claims extracted)")

    print("\n--- Answer Relevancy (reference-free, all answerable rows) ---")
    if ar_scores:
        print(f"Mean per-question answer relevancy: {Ar['mean_per_question']:.3f}  (n={Ar['n_scored']})")
    else:
        print("Mean per-question answer relevancy: n/a (no scorable answers)")
    if ar_total > 0:
        print(f"Micro answer relevancy (statements): {ar_relevant}/{ar_total} = {Ar['micro_ratio']:.3f}")
    else:
        print("Micro answer relevancy (statements): n/a (no statements extracted)")

    print(f"\n--- Context Relevancy (reference-free, {len(faith_indices)}/{len(judged_inputs)} sampled, {FAITHFULNESS_FRACTION:.0%}) ---")
    if cr_scores:
        print(f"Mean per-question context relevancy: {Cr['mean_per_question']:.3f}  (n={Cr['n_scored']})")
    else:
        print("Mean per-question context relevancy: n/a (no scorable context)")
    if cr_total > 0:
        print(f"Micro context relevancy (statements): {cr_relevant}/{cr_total} = {Cr['micro_ratio']:.3f}")
    else:
        print("Micro context relevancy (statements): n/a (no statements extracted)")

    # --- Refusal / abstention slice ---
    print("\n--- Refusal / Abstention ---")
    print(f"Unanswerable questions in dataset: {unanswerable_total}")
    if unanswerable_no_response:
        print(f"  ({unanswerable_no_response} skipped due to API errors)")
    if valid_unans:
        print(f"Abstention rate (correct):    {Rf['abstention_rate']:.3f}  ({Rf['abstention_count']}/{Rf['abstention_denom']})")
        print(f"Confabulation rate (failure): {1 - Rf['abstention_rate']:.3f}  ({Rf['abstention_denom'] - Rf['abstention_count']}/{Rf['abstention_denom']})")
    elif unanswerable_total > 0:
        print("Abstention rate: n/a (judge errors on every entry)")
    else:
        print("No unanswerable questions in dataset — add some via generate_questions.py to enable this metric.")

    if topic_seed_total:
        rate_str = f"  ({topic_retrieval_rate:.3f})" if topic_retrieval_rate is not None else ""
        print(f"Topic chunk retrieved:        {topic_seed_retrieved}/{topic_seed_total} unanswerable rows{rate_str}")
        if topic_seed_retrieved:
            print(
                f"Abstention when topic retrieved: {abstention_given_topic_retrieved:.3f}  "
                f"({topic_retrieved_abstained}/{topic_seed_retrieved}) "
                "— correctly abstained despite relevant-looking context"
            )
        else:
            print("Abstention when topic retrieved: n/a (retriever surfaced none of the seed chunks)")

    if valid_ans:
        print(f"False-refusal rate on answerable: {Rf['false_refusal_rate']:.3f}  ({Rf['false_refusal_count']}/{Rf['false_refusal_denom']})")

    print("\n--- Response Logprobs ---")
    if valid_mean_lps:
        print(f"Mean token logprob: {Lp['mean_logprob_mean']:.4f}  (n={Lp['n_scored']})")
        print(f"Median token logprob: {Lp['mean_logprob_median']:.4f}")
        print(f"Mean perplexity:     {Lp['perplexity_mean']:.3f}")
        print(f"Median perplexity:   {Lp['perplexity_median']:.3f}")
    else:
        print("No logprobs returned by server — confirm logprobs forwarding is enabled.")

    print("\n--- Latency Stats (seconds) ---")
    print(f"Mean: {_fmt(La['mean'])}")
    print(f"Median (p50): {_fmt(La['p50'])}")

    # Print evaluation distributions
    print("\n", "-" * 50, "\n")
    print("\nCompletion evaluation distribution:")
    print(eval_results["completion_evaluation"].value_counts())
    print(f"Completion evaluation average: {eval_results['completion_evaluation'].mean():.3f}")
    print("\n", "-" * 50, "\n")
    print("\nPrecision evaluation distribution:")
    print(eval_results["precision_evaluation"].value_counts())
    print(f"Precision evaluation average: {eval_results['precision_evaluation'].mean():.3f}")

    print("\n", "-" * 50, "\n")
    print(f"--- Response Labels ({LABEL_LANGUAGE}) ---")
    label_only = [lbl for lbl, _ in label_results]
    valid_labels = [lbl for lbl in label_only if lbl != JUDGE_ERROR]
    if valid_labels:
        label_counts = pd.Series(valid_labels).value_counts()
        for lbl in RESPONSE_LABELS:
            count = int(label_counts.get(lbl, 0))
            pct = count / len(valid_labels) if valid_labels else 0.0
            print(f"  {lbl:<16} {count:>4}  ({pct:.1%})")
        n_errors = len(label_only) - len(valid_labels)
        if n_errors:
            print(f"  (errors: {n_errors})")

        os.makedirs(output_dir, exist_ok=True)
        labels_path = os.path.join(output_dir, f"response_labels_{run_ts}.csv")
        labels_dump = [
            {
                "question": judged_inputs[i]["question"],
                "llm_answer": judged_inputs[i]["llm_answer"],
                "openrag_answer": judged_responses[i],
                "label": label_results[i][0],
                "label_reasoning": label_results[i][1],
                "cheap_completion": llm_judge_scores[i][0],
                "cheap_precision": llm_judge_scores[i][1],
            }
            for i in range(len(judged_inputs))
        ]
        pd.DataFrame(labels_dump).to_csv(labels_path, index=False)
        print(f"Response labels written to {labels_path}")
    else:
        print("Response labels: no valid labels (all judge calls errored).")

    print("\n", "-" * 50, "\n")
    print(f"--- CoT Evaluation ({COT_AUDIT_FRACTION:.0%} random sample, seed={COT_AUDIT_SEED}) ---")
    print(f"Sample size: {len(audit_indices)} of {len(judged_inputs)} questions")
    valid_cot = [(i, cot) for i, cot in zip(audit_indices, cot_results) if cot[0] != JUDGE_ERROR]
    if valid_cot:
        cot_comp = np.array([c[0] for _, c in valid_cot])
        cot_prec = np.array([c[1] for _, c in valid_cot])
        print(f"CoT completion mean: {cot_comp.mean():.3f}")
        print(f"CoT precision mean:  {cot_prec.mean():.3f}")

        audit_dump = [
            {
                "question": judged_inputs[i]["question"],
                "llm_answer": judged_inputs[i]["llm_answer"],
                "openrag_answer": judged_responses[i],
                "cheap_completion": llm_judge_scores[i][0],
                "cheap_precision": llm_judge_scores[i][1],
                "cot_completion": cot[0],
                "cot_precision": cot[1],
                "cot_completion_reasoning": cot[2],
                "cot_precision_reasoning": cot[3],
            }
            for i, cot in valid_cot
        ]
        os.makedirs(output_dir, exist_ok=True)
        audit_path = os.path.join(output_dir, f"cot_audit_{run_ts}.csv")
        pd.DataFrame(audit_dump).to_csv(audit_path, index=False)
        print(f"CoT evaluation details written to {audit_path}")
    elif audit_indices:
        print("CoT evaluation: all sampled judgments errored.")
    else:
        print("CoT evaluation: no questions to sample.")

    # --- Judge agreement / calibration ---
    # Cross-tab label × {completion, precision} buckets and correlate per-row
    # perplexity with both score axes. All four signals are 1:1 with judged_inputs.
    print("\n", "-" * 50, "\n")
    print("--- Judge Agreement / Calibration ---")

    calib_summary: dict = {"n_rows": 0}
    if label_results and llm_judge_scores:
        calib_df = pd.DataFrame({
            "label": [lbl for lbl, _ in label_results],
            "completion": [s[0] for s in llm_judge_scores],
            "precision": [s[1] for s in llm_judge_scores],
            "perplexity": perplexities[: len(label_results)],
            "mean_logprob": mean_logprobs[: len(label_results)],
        })
        calib_df = calib_df[
            (calib_df["label"] != JUDGE_ERROR)
            & (calib_df["completion"] != JUDGE_ERROR)
            & (calib_df["precision"] != JUDGE_ERROR)
        ].copy()
        calib_df["completion"] = pd.to_numeric(calib_df["completion"], errors="coerce")
        calib_df["precision"] = pd.to_numeric(calib_df["precision"], errors="coerce")
        calib_df = calib_df.dropna(subset=["completion", "precision"])
    else:
        calib_df = pd.DataFrame()

    if len(calib_df):
        def _bucket(s: float) -> str:
            if s >= 9:
                return "9-10"
            if s >= 7:
                return "7-8"
            if s >= 5:
                return "5-6"
            return "1-4"

        calib_df["completion_bucket"] = calib_df["completion"].apply(_bucket)
        calib_df["precision_bucket"] = calib_df["precision"].apply(_bucket)

        bucket_order = ["1-4", "5-6", "7-8", "9-10"]
        label_order = list(RESPONSE_LABELS)
        comp_xtab = pd.crosstab(calib_df["label"], calib_df["completion_bucket"]).reindex(
            index=label_order, columns=bucket_order, fill_value=0
        )
        prec_xtab = pd.crosstab(calib_df["label"], calib_df["precision_bucket"]).reindex(
            index=label_order, columns=bucket_order, fill_value=0
        )

        print(f"Rows scored by all three judges: {len(calib_df)}\n")
        print("Label vs. completion bucket:")
        print(comp_xtab)
        print("\nLabel vs. precision bucket:")
        print(prec_xtab)

        fully_correct = calib_df[calib_df["label"] == "Fully Correct"]
        contradictory = calib_df[calib_df["label"] == "Contradictory"]
        fc_low = fully_correct[(fully_correct["completion"] < 7) | (fully_correct["precision"] < 7)]
        contra_high = contradictory[(contradictory["completion"] >= 7) & (contradictory["precision"] >= 7)]
        print(
            "\nDisagreements (loud signals of judge inconsistency):"
            f"\n  'Fully Correct' with completion or precision <7: {len(fc_low)}/{len(fully_correct)}"
            f"\n  'Contradictory' with completion AND precision >=7: {len(contra_high)}/{len(contradictory)}"
        )

        calib_summary = {
            "n_rows": int(len(calib_df)),
            "label_vs_completion": {
                lbl: {b: int(comp_xtab.loc[lbl, b]) for b in bucket_order} for lbl in label_order
            },
            "label_vs_precision": {
                lbl: {b: int(prec_xtab.loc[lbl, b]) for b in bucket_order} for lbl in label_order
            },
            "fully_correct_total": int(len(fully_correct)),
            "fully_correct_with_low_score": int(len(fc_low)),
            "contradictory_total": int(len(contradictory)),
            "contradictory_with_high_score": int(len(contra_high)),
        }

        ppl_df = calib_df.dropna(subset=["perplexity"])
        if len(ppl_df):
            ppl_by_label = (
                ppl_df.groupby("label")["perplexity"]
                .agg(["count", "mean", "median"])
                .round(3)
                .reindex(label_order)
            )
            print("\nPerplexity per label (lower = more confident):")
            print(ppl_by_label)
            calib_summary["perplexity_by_label"] = {
                lbl: float(ppl_by_label.loc[lbl, "mean"])
                for lbl in label_order
                if not math.isnan(ppl_by_label.loc[lbl, "mean"])
            }

            if len(ppl_df) >= 3:
                pearson = ppl_df[["completion", "precision", "perplexity", "mean_logprob"]].corr(method="pearson")
                spearman = ppl_df[["completion", "precision", "perplexity", "mean_logprob"]].corr(method="spearman")
                print("\nScore ↔ perplexity correlations (expect negative — confident answers score higher):")
                print(f"  Pearson  completion↔perplexity: {pearson.loc['completion', 'perplexity']:+.3f}")
                print(f"  Spearman completion↔perplexity: {spearman.loc['completion', 'perplexity']:+.3f}")
                print(f"  Pearson  precision↔perplexity:  {pearson.loc['precision', 'perplexity']:+.3f}")
                print(f"  Spearman precision↔perplexity:  {spearman.loc['precision', 'perplexity']:+.3f}")
                calib_summary.update({
                    "pearson_completion_perplexity": float(pearson.loc["completion", "perplexity"]),
                    "spearman_completion_perplexity": float(spearman.loc["completion", "perplexity"]),
                    "pearson_precision_perplexity": float(pearson.loc["precision", "perplexity"]),
                    "spearman_precision_perplexity": float(spearman.loc["precision", "perplexity"]),
                })
    else:
        print("Calibration: not enough scored rows (need label + completion + precision judges).")

    # --- Save timestamped report ---
    # metrics_summary (computed once above) is the single source for every
    # aggregate; the JSON family blocks below reference it directly.
    summary_dict = {
        "timestamp": run_ts,
        "config": {
            # Version-comparison identity: which OpenRAG build produced these numbers.
            # Populated by orchestrator.py; None for plain (non-orchestrated) runs.
            "run_id": run_id,
            "version": version,
            "git_commit": git_commit,
            "no_retrieval": no_retrieval,
            "partition": partition,
            "base_url": openrag_api_base_url,
            "dataset_path": dataset_path,
            "dataset_size": len(list_response_answer_reference),
            "dataset_schema_version": (judged_inputs[0].get("metadata", {}) or {}).get("schema_version")
            if judged_inputs
            else None,
            "label_language": LABEL_LANGUAGE,
            "cot_audit_fraction": COT_AUDIT_FRACTION,
            "judge_model": _judge_model,
            "judge_base_url": _judge_base_url,
        },
        "retrieval": metrics_summary["retrieval"],
        "generation": metrics_summary["generation"],
        "breakdowns": score_breakdowns,
        "judge_scores": {
            "completion_mean": float(eval_results["completion_evaluation"].mean()) if len(eval_results) else None,
            "precision_mean": float(eval_results["precision_evaluation"].mean()) if len(eval_results) else None,
            "completion_distribution": {
                int(k): int(v) for k, v in eval_results["completion_evaluation"].value_counts().items()
            } if len(eval_results) else {},
            "precision_distribution": {
                int(k): int(v) for k, v in eval_results["precision_evaluation"].value_counts().items()
            } if len(eval_results) else {},
        },
        "faithfulness": metrics_summary["faithfulness"],
        "answer_relevancy": metrics_summary["answer_relevancy"],
        "context_relevancy": metrics_summary["context_relevancy"],
        "refusal": metrics_summary["refusal"],
        "latency": metrics_summary["latency"],
        "logprobs": metrics_summary["logprobs"],
        "labels": {
            lbl: int(pd.Series([lab for lab, _ in label_results] if label_results else []).value_counts().get(lbl, 0))
            for lbl in RESPONSE_LABELS
        },
        "label_errors": sum(1 for lab, _ in label_results if lab == JUDGE_ERROR) if label_results else 0,
        "cot_audit": {
            "sample_size": len(audit_indices),
            "valid_size": len([c for c in cot_results if c[0] != JUDGE_ERROR]) if cot_results else 0,
            "completion_mean": float(np.mean([c[0] for c in cot_results if c[0] != JUDGE_ERROR]))
                if any(c[0] != JUDGE_ERROR for c in cot_results) else None,
            "precision_mean": float(np.mean([c[1] for c in cot_results if c[0] != JUDGE_ERROR]))
                if any(c[0] != JUDGE_ERROR for c in cot_results) else None,
        },
        "calibration": calib_summary,
        "artifacts": {
            "per_question_csv": f"eval_{run_ts}.csv",
            "labels_csv": f"response_labels_{run_ts}.csv" if label_results else None,
            "cot_audit_csv": f"cot_audit_{run_ts}.csv" if any(c[0] != JUDGE_ERROR for c in cot_results) else None,
            "logprobs_jsonl": f"logprobs_{run_ts}.jsonl" if any(token_sequences) else None,
            "summary_txt": f"eval_{run_ts}.txt",
        },
    }

    # Per-question token-level logprob dump (one JSON object per line, aligned with
    # judged_inputs). Drives the dashboard's per-question logprob explorer.
    if any(token_sequences):
        logprobs_path = os.path.join(output_dir, f"logprobs_{run_ts}.jsonl")
        with open(logprobs_path, "w", encoding="utf-8") as f:
            for i, inp in enumerate(judged_inputs):
                tokens = token_sequences[i] if i < len(token_sequences) else []
                lps = token_logprob_sequences[i] if i < len(token_logprob_sequences) else []
                if not tokens:
                    continue
                row = {
                    "question": inp.get("question", ""),
                    "mean_logprob": mean_logprobs[i] if i < len(mean_logprobs) else None,
                    "perplexity": perplexities[i] if i < len(perplexities) else None,
                    "tokens": tokens,
                    "logprobs": lps,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Per-question token logprobs written to {logprobs_path}")

    json_path = os.path.join(output_dir, f"eval_{run_ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary_dict, f, indent=2, ensure_ascii=False)
    print(f"\nStructured summary written to {json_path}")

    summary_lines = [
        "==== Evaluation Report ====",
        f"Average Hit Rate: {round(R['hit_rate'], 3) if R['hit_rate'] is not None else 'n/a'}",
        f"Average MRR: {round(R['mrr'], 3) if R['mrr'] is not None else 'n/a'}",
        f"Average Recall: {round(R['recall'], 3) if R['recall'] is not None else 'n/a'}",
        f"Precision@5: {_fmt(R['precision_at_5'])}",
        f"Precision@10: {_fmt(R['precision_at_10'])}",
        f"MAP: {_fmt(R['map'])}",
        f"R-Precision: {_fmt(R['r_precision'])}",
        f"Recall@5: {_fmt(R['recall_at_5'])}",
        f"Recall@10: {_fmt(R['recall_at_10'])}",
        f"nDCG@5: {_fmt(R['ndcg_at_5'])}",
        f"nDCG@10: {_fmt(R['ndcg_at_10'])}",
        f"Average nDCG per chunk count: {avg_ndcg_per_chunk}" if len(ndcg_scored) else "Average nDCG per chunk count: n/a",
        f"Average nDCG: {round(R['ndcg_mean'], 3)} +/- {R['ndcg_std']:.3f}" if len(ndcg_scored) else "Average nDCG: n/a",
        "",
        "--- Generation Metrics ---",
        f"ROUGE-1: {_fmt(G['rouge1'])}",
        f"ROUGE-2: {_fmt(G['rouge2'])}",
        f"ROUGE-L: {_fmt(G['rougeL'])}",
        f"BLEU: {_fmt(G['bleu'])}",
        f"METEOR: {_fmt(G['meteor'])}",
        "",
        f"--- Faithfulness ({len(faith_indices)}/{len(judged_inputs)} sampled, {FAITHFULNESS_FRACTION:.0%}) ---",
        f"Mean per-question faithfulness: {Fa['mean_per_question']:.3f} (n={Fa['n_scored']})" if faith_scores else "Mean per-question faithfulness: n/a",
        f"Micro faithfulness (claims): {total_supported}/{total_claims} = {Fa['micro_ratio']:.3f}" if total_claims > 0 else "Micro faithfulness: n/a",
        "",
        "--- Answer Relevancy (reference-free, all answerable rows) ---",
        f"Mean per-question answer relevancy: {Ar['mean_per_question']:.3f} (n={Ar['n_scored']})" if ar_scores else "Mean per-question answer relevancy: n/a",
        f"Micro answer relevancy (statements): {ar_relevant}/{ar_total} = {Ar['micro_ratio']:.3f}" if ar_total > 0 else "Micro answer relevancy: n/a",
        "",
        f"--- Context Relevancy (reference-free, {len(faith_indices)}/{len(judged_inputs)} sampled, {FAITHFULNESS_FRACTION:.0%}) ---",
        f"Mean per-question context relevancy: {Cr['mean_per_question']:.3f} (n={Cr['n_scored']})" if cr_scores else "Mean per-question context relevancy: n/a",
        f"Micro context relevancy (statements): {cr_relevant}/{cr_total} = {Cr['micro_ratio']:.3f}" if cr_total > 0 else "Micro context relevancy: n/a",
        "",
        "--- Refusal / Abstention ---",
        f"Unanswerable questions: {unanswerable_total}",
        f"Abstention rate: {Rf['abstention_rate']:.3f} ({Rf['abstention_count']}/{Rf['abstention_denom']})" if valid_unans else "Abstention rate: n/a",
        f"Topic chunk retrieved: {topic_seed_retrieved}/{topic_seed_total} ({topic_retrieval_rate:.3f})" if topic_seed_total else "Topic chunk retrieved: n/a",
        f"Abstention when topic retrieved: {abstention_given_topic_retrieved:.3f} ({topic_retrieved_abstained}/{topic_seed_retrieved})" if topic_seed_retrieved else "Abstention when topic retrieved: n/a",
        f"False-refusal rate on answerable: {Rf['false_refusal_rate']:.3f} ({Rf['false_refusal_count']}/{Rf['false_refusal_denom']})" if valid_ans else "False-refusal rate: n/a",
        "",
        "--- Response Logprobs ---",
        f"Mean token logprob: {Lp['mean_logprob_mean']:.4f} (n={Lp['n_scored']})" if valid_mean_lps else "Mean token logprob: n/a",
        f"Median token logprob: {Lp['mean_logprob_median']:.4f}" if valid_mean_lps else "Median token logprob: n/a",
        f"Mean perplexity: {Lp['perplexity_mean']:.3f}" if valid_ppls else "Mean perplexity: n/a",
        f"Median perplexity: {Lp['perplexity_median']:.3f}" if valid_ppls else "Median perplexity: n/a",
        "",
        "--- Latency Stats (seconds) ---",
        f"Mean: {_fmt(La['mean'])}",
        f"Median (p50): {_fmt(La['p50'])}",
        f"p95: {_fmt(La['p95'])}",
        f"p99: {_fmt(La['p99'])}",
        "",
        "-" * 50,
        "Completion evaluation distribution:",
        str(eval_results["completion_evaluation"].value_counts()),
        f"Average: {eval_results['completion_evaluation'].mean():.3f}",
        "",
        "-" * 50,
        "Precision evaluation distribution:",
        str(eval_results["precision_evaluation"].value_counts()),
        f"Average: {eval_results['precision_evaluation'].mean():.3f}",
    ]

    txt_path = os.path.join(output_dir, f"eval_{run_ts}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))
    print(f"\nSummary report written to {txt_path}")

    csv_path = os.path.join(output_dir, f"eval_{run_ts}.csv")
    eval_results.to_csv(csv_path, index=False)
    print(f"Per-question scores written to {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the RAG evaluation benchmark.")
    parser.add_argument("--partition", default=CONFIG.common.partition, help="OpenRAG partition to query")
    parser.add_argument("--base-url", default=os.environ.get("API_BASE_URL"), help="OpenRAG base URL")
    parser.add_argument(
        "--dataset",
        default=None,
        help="Path to dataset JSON. Precedence: --dataset > GOLDEN_DATASET env var > config.common.dataset_path",
    )
    parser.add_argument("--output-dir", default=CONFIG.common.output_dir, help="Directory for report artifacts")
    parser.add_argument("--limit", type=int, default=CONFIG.benchmark.limit, help="Limit dataset to N entries (debug)")
    parser.add_argument("--run-id", default=None, help="Unique run identifier (set by orchestrator.py for version comparison)")
    parser.add_argument("--version", default=None, help="Git tag/branch/commit of the OpenRAG build under evaluation")
    parser.add_argument("--git-commit", default=None, help="Resolved git commit SHA of the OpenRAG build under evaluation")
    parser.add_argument(
        "--no-retrieval",
        action="store_true",
        help="Skip ID-based retrieval metrics even if the dataset has ground-truth chunks "
        "(generation + judges still run). Use when chunk IDs don't match the evaluated index.",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="After the benchmark, run the context-contribution ablation (closed-book vs OpenRAG).",
    )
    parser.add_argument(
        "--ablation-limit",
        type=int,
        default=CONFIG.benchmark.ablation_limit,
        help="Questions to sample for the ablation. Only used with --ablation.",
    )
    args = parser.parse_args()

    resolved_dataset = args.dataset or os.environ.get("GOLDEN_DATASET") or CONFIG.common.dataset_path

    asyncio.run(
        main(
            partition=args.partition,
            base_url=args.base_url,
            dataset_path=resolved_dataset,
            output_dir=args.output_dir,
            limit=args.limit,
            run_id=args.run_id,
            version=args.version,
            git_commit=args.git_commit,
            no_retrieval=args.no_retrieval,
        )
    )

    if args.ablation:
        # Lazy import.
        import context_ablation

        print("\n" + "=" * 50)
        print("Running context-contribution ablation (closed-book vs OpenRAG)…")
        asyncio.run(
            context_ablation.main(
                partition=args.partition,
                base_url=args.base_url,
                dataset_path=resolved_dataset,
                output_dir=args.output_dir,
                limit=args.ablation_limit,
            )
        )
