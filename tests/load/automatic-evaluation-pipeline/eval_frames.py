"""
Evaluate OpenRAG on the FRAMES benchmark (google/frames-benchmark).

FRAMES contains 824 multi-hop questions requiring 2-15 Wikipedia articles each.
Run `setup_frames.py` first to download articles and index them into OpenRAG.

Modes:
  - Default: queries OpenRAG (retriever + LLM generation)
  - --no-rag: bypasses retrieval, sends prompt directly to the LLM without chunks
  - --oracle: bypasses retrieval, provides gold Wikipedia articles to the LLM
  - --gold-workspaces: creates one workspace per question, attaches only gold files

Usage:
    cd automatic-evaluation-pipeline
    python eval_frames.py [--partition FRAMES] [--output results.json] [--limit N]
    python eval_frames.py --no-rag
    python eval_frames.py --oracle
    python eval_frames.py --gold-workspaces [--partition FRAMES]

Environment variables (from .env):
    APP_URL, APP_PORT, AUTH_TOKEN, MODEL, BASE_URL, API_KEY
    MODEL_JUDGE, BASE_URL_JUDGE, API_KEY_JUDGE
"""

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import string
import warnings
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from loguru import logger
from pydantic import BaseModel, Field
from tqdm.asyncio import tqdm

load_dotenv()

warnings.filterwarnings("ignore", message=r".*Pydantic serializer warnings.*")

# ─── Env / config ────────────────────────────────────────────────────────────

APP_URL = os.environ.get("APP_URL", "localhost")
APP_PORT = os.environ.get("APP_PORT", "8080")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "sk-1234")
MODEL = os.environ.get("MODEL")
BASE_URL = os.environ.get("BASE_URL")
API_KEY = os.environ.get("API_KEY")
JUDGE_MODEL = os.environ.get("MODEL_JUDGE", MODEL)
JUDGE_BASE_URL = os.environ.get("BASE_URL_JUDGE", BASE_URL)
JUDGE_API_KEY = os.environ.get("API_KEY_JUDGE", API_KEY)

OPENRAG_BASE_URL = f"http://{APP_URL}:{APP_PORT}"

MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 5, 10]


def _require_llm_config(*, judge_only: bool = False) -> None:
    """Fail fast with a clear message when the LLM env vars are missing.

    ChatOpenAI(model=None, base_url=None, ...) otherwise fails deep inside the
    client with an opaque error. The --no-rag and --oracle modes need MODEL /
    BASE_URL / API_KEY; every mode that runs the judge needs the JUDGE_* values
    (which default to the primary ones).
    """
    pairs = [("MODEL_JUDGE", JUDGE_MODEL), ("BASE_URL_JUDGE", JUDGE_BASE_URL), ("API_KEY_JUDGE", JUDGE_API_KEY)]
    if not judge_only:
        pairs = [("MODEL", MODEL), ("BASE_URL", BASE_URL), ("API_KEY", API_KEY), *pairs]
    missing = [name for name, val in pairs if not val]
    if missing:
        raise SystemExit(f"ERROR: missing required environment variable(s): {', '.join(missing)}")


async def _http_with_retry(
    client: httpx.AsyncClient, method: str, url: str, *, max_retries: int = MAX_RETRIES, **kwargs
) -> httpx.Response:
    """HTTP request with retry on 5xx errors and network errors."""
    for attempt in range(max_retries + 1):
        try:
            if method == "GET":
                resp = await client.get(url, **kwargs)
            else:
                resp = await client.post(url, **kwargs)
            if resp.status_code >= 500 and attempt < max_retries:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.debug(f"HTTP {resp.status_code} for {url}, retry {attempt+1}/{max_retries} in {wait}s")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError:
            raise
        except Exception:
            if attempt == max_retries:
                raise
            wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
            logger.debug(f"Network error for {url}, retry {attempt+1}/{max_retries} in {wait}s")
            await asyncio.sleep(wait)


async def _check_openrag_health(client: httpx.AsyncClient) -> bool:
    """Check OpenRAG is reachable. Returns True if up, False otherwise."""
    try:
        resp = await client.get(f"{OPENRAG_BASE_URL}/health_check")
        resp.raise_for_status()
        print("OpenRAG API is up.")
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach OpenRAG at {OPENRAG_BASE_URL}: {e}")
        return False


# ─── Dataset caching ────────────────────────────────────────────────────────

DATASET_CACHE = Path(__file__).parent / "frames_dataset.json"
DEFAULT_UNANSWERABLE_EXACT_MATCH_ANSWERS = [
    "unanswerable",
    "cannot answer",
    "i cannot answer",
    "the answer is not in the provided context",
    "the answer is not in the context",
    "not enough information in the provided context",
]


def _default_dataset_cache(dataset_path: str | None) -> Path:
    if dataset_path:
        path = Path(dataset_path)
        return path if path.is_absolute() else Path(__file__).parent / dataset_path
    return DATASET_CACHE


def load_dataset_cached(limit: int | None = None, dataset_path: str | None = None) -> list[dict]:
    """Load dataset from local cache path, or download FRAMES and cache it there."""
    cache_path = _default_dataset_cache(dataset_path)
    if cache_path.exists():
        logger.info(f"Loading dataset from cache ({cache_path.name})...")
        with open(cache_path, encoding="utf-8") as f:
            dataset = json.load(f)
    else:
        logger.info("Downloading FRAMES benchmark from HuggingFace (first time only)...")
        from datasets import load_dataset
        hf_dataset = load_dataset("google/frames-benchmark", split="test")
        dataset = [dict(row) for row in hf_dataset]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataset, f, ensure_ascii=False, indent=2)
        logger.info(f"Cached {len(dataset)} questions to {cache_path.name}")

    logger.info(f"Loaded {len(dataset)} questions.")
    if limit:
        dataset = dataset[:limit]
        logger.info(f"Limited to {len(dataset)} questions.")
    return dataset


# ─── Wikipedia helpers ───────────────────────────────────────────────────────


def extract_title_from_url(url: str) -> str | None:
    """Extract the Wikipedia article title from a URL."""
    parsed = urlparse(url)
    if "wikipedia.org" not in parsed.netloc:
        return None
    match = re.match(r"/wiki/(.+)", parsed.path)
    if match:
        return unquote(match.group(1)).replace("_", " ")
    return None


def parse_wiki_links(row) -> list[str]:
    """Extract Wikipedia URLs from a FRAMES dataset row."""
    if row.get("wiki_links") is None:
        return []

    def _split_embedded_wiki_urls(value: str) -> list[str]:
        pattern = r"https?://(?:[\w.-]+\.)?wikipedia\.org/wiki/"
        starts = [m.start() for m in re.finditer(pattern, value)]
        if not starts:
            return []
        if len(starts) == 1:
            return [value.strip().strip(",")]

        urls = []
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(value)
            candidate = value[start:end].strip().strip(",").strip("'").strip('"')
            if candidate:
                urls.append(candidate)
        return urls

    wiki_links = row.get("wiki_links")
    if not wiki_links:
        return []
    if isinstance(wiki_links, str):
        try:
            links = ast.literal_eval(wiki_links)
        except (ValueError, SyntaxError):
            try:
                links = json.loads(wiki_links)
            except json.JSONDecodeError:
                links = [part.strip() for part in wiki_links.split(",") if part.strip()]
    else:
        links = wiki_links

    normalized_links = []
    for link in links:
        if not isinstance(link, str) or "wikipedia.org" not in link:
            continue
        normalized_links.extend(_split_embedded_wiki_urls(link))

    seen = set()
    deduped = []
    for link in normalized_links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped


def _safe_filename(title: str) -> str:
    """Convert a Wikipedia title to a safe filename.

    Uses a stable hash as a last resort so the fallback name matches the one
    setup_frames.py wrote on disk (Python's built-in hash() is salted per
    process, which would break oracle / gold-file matching across runs).
    """
    safe_name = re.sub(r'[^\w\s\-()]', '', title).strip()
    safe_name = re.sub(r'\s+', '_', safe_name)
    if not safe_name:
        safe_name = f"article_{hashlib.md5(title.encode('utf-8')).hexdigest()[:8]}"
    return safe_name


def _strip_html_for_oracle(html: str) -> str:
    """Lightweight HTML cleanup for oracle context."""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    text = re.sub(r'<(\w+)\s[^>]*?(/?)>', r'<\1\2>', text)
    text = re.sub(r'<(\w+)>\s*</\1>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def load_articles_from_disk(rows: list[dict], docs_dir: Path) -> dict[str, str]:
    """Load Wikipedia articles from local .md or .html files for oracle mode."""
    all_titles: set[str] = set()
    for row in rows:
        for url in parse_wiki_links(row):
            title = extract_title_from_url(url)
            if title:
                all_titles.add(title)

    # .md takes priority when both exist; .html is stripped of markup before use.
    md_files = {p.stem: p for p in docs_dir.glob("*.md")}
    html_files = {p.stem: p for p in docs_dir.glob("*.html")}

    articles = {}
    missing = []
    for title in all_titles:
        safe_name = _safe_filename(title)
        if safe_name in md_files:
            articles[title] = md_files[safe_name].read_text(encoding="utf-8")
        elif safe_name in html_files:
            articles[title] = _strip_html_for_oracle(html_files[safe_name].read_text(encoding="utf-8"))
        else:
            missing.append(title)

    print(f"Loaded {len(articles)} articles from disk ({len(missing)} missing).")
    if missing:
        logger.info(f"Missing articles: {missing[:20]}{'...' if len(missing) > 20 else ''}")
    return articles


# ─── OpenRAG API calls ────────────────────────────────────────────────────────


async def _fetch_chunk_content(
    client: httpx.AsyncClient,
    chunk_url: str,
    headers: dict,
) -> str:
    """Fetch the text content of a single chunk via its extract URL."""
    try:
        resp = await _http_with_retry(client, "GET", chunk_url, headers=headers)
        return resp.json().get("page_content", "")
    except Exception as e:
        logger.debug(f"Failed to fetch chunk {chunk_url}: {e}")
        return ""


async def query_openrag_answer(
    question: str,
    partition: str,
    semaphore: asyncio.Semaphore,
    workspace: str | None = None,
) -> tuple[str | None, str]:
    """Call OpenRAG chat completions for a single question."""
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}

    async with semaphore:
        async with httpx.AsyncClient(timeout=300) as client:
            payload = {
                "model": f"openrag-{partition}",
                "messages": [{"role": "user", "content": question}],
                "temperature": 0.1,
                "max_tokens": 512,
                "stream": False,
            }
            if workspace:
                payload["metadata"] = {"workspace": workspace}

            try:
                chat_resp = await _http_with_retry(
                    client, "POST", f"{OPENRAG_BASE_URL}/v1/chat/completions",
                    json=payload, headers=headers,
                )
                body = chat_resp.json()
                generated_answer = body["choices"][0]["message"]["content"]
            except Exception as e:
                logger.debug(f"Chat completions error for question {question!r}: {e}")
                return None, ""

            sources_content = ""
            try:
                extra = body.get("extra", "")
                if isinstance(extra, str) and extra:
                    extra = json.loads(extra)
                sources = extra.get("sources", []) if isinstance(extra, dict) else []
                chunk_urls = [s["chunk_url"] for s in sources if "chunk_url" in s]

                if chunk_urls:
                    chunk_tasks = [_fetch_chunk_content(client, url, headers) for url in chunk_urls]
                    chunks = await asyncio.gather(*chunk_tasks)
                    sources_content = "\n\n".join(c for c in chunks if c)
            except Exception as e:
                logger.debug(f"Failed to extract sources for question {question!r}: {e}")

    return generated_answer, sources_content


def _build_gold_name(prefix: str, row_index: int) -> str:
    """Build a deterministic name for a per-question gold workspace."""
    return f"{prefix}-q{row_index:04d}"


def _get_gold_file_ids(row: dict) -> list[str]:
    """Return the normalized gold file ids referenced by one FRAMES row."""
    file_ids = []
    for url in parse_wiki_links(row):
        title = extract_title_from_url(url)
        if title:
            file_ids.append(_safe_filename(title))
    return file_ids


async def ensure_workspace(
    client: httpx.AsyncClient,
    partition: str,
    workspace_id: str,
) -> bool:
    """Create a workspace if needed."""
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    payload = {"workspace_id": workspace_id, "display_name": workspace_id}
    resp = await client.post(f"{OPENRAG_BASE_URL}/partition/{partition}/workspaces", json=payload, headers=headers)
    if resp.status_code == 201:
        return True
    if resp.status_code == 409:
        return False
    resp.raise_for_status()
    return False


async def add_files_to_workspace(
    client: httpx.AsyncClient,
    partition: str,
    workspace_id: str,
    file_ids: list[str],
) -> None:
    """Attach files to a workspace.

    Tolerant of partial failures: some gold articles may have failed to download
    or index, so a rejected attachment is logged and skipped rather than aborting
    the whole evaluation run.
    """
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    resp = await client.post(
        f"{OPENRAG_BASE_URL}/partition/{partition}/workspaces/{workspace_id}/files",
        json={"file_ids": file_ids},
        headers=headers,
    )
    if resp.status_code not in (200, 201):
        logger.warning(
            f"Could not attach files to workspace '{workspace_id}': {resp.status_code} - {resp.text[:200]}"
        )


async def prepare_gold_workspace_for_question(
    client: httpx.AsyncClient,
    row_index: int,
    row: dict,
    source_partition: str,
    workspace_prefix: str,
) -> tuple[str, list[str]]:
    """Create one question workspace and attach its gold files."""
    workspace_id = _build_gold_name(workspace_prefix, row_index)
    file_ids = _get_gold_file_ids(row)
    await ensure_workspace(client, source_partition, workspace_id)
    if file_ids:
        await add_files_to_workspace(client, source_partition, workspace_id, file_ids)
    return workspace_id, file_ids


async def get_available_openrag_partitions(client: httpx.AsyncClient) -> list[str]:
    """List partitions exposed as OpenRAG models."""
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    resp = await _http_with_retry(client, "GET", f"{OPENRAG_BASE_URL}/v1/models", headers=headers)
    models = resp.json().get("data", [])
    return [
        m["id"].removeprefix("openrag-")
        for m in models
        if m.get("id", "").startswith("openrag-") and m["id"] != "openrag-all"
    ]


async def get_available_workspaces(client: httpx.AsyncClient, partition: str) -> list[str]:
    """List workspaces accessible in a partition."""
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    resp = await _http_with_retry(
        client, "GET", f"{OPENRAG_BASE_URL}/partition/{partition}/workspaces", headers=headers
    )
    return [ws["workspace_id"] for ws in resp.json().get("workspaces", []) if ws.get("workspace_id")]


# ─── Oracle / No-RAG modes ──────────────────────────────────────────────────

_oracle_system_prompt = """\
You are a helpful assistant. Answer the user's question based on the provided Wikipedia articles. \
The articles are provided as cleaned HTML — pay close attention to tables and structured data. \
Use the information from the articles to reason step by step and provide an accurate, concise answer.

If the answer cannot be determined from the provided context, reply with exactly:
unanswerable
"""


def build_oracle_context(
    row,
    articles: dict[str, str],
    max_chars: int = 0,
) -> str:
    """Build the oracle context string from gold Wikipedia articles."""
    parts = []
    for url in parse_wiki_links(row):
        title = extract_title_from_url(url)
        if title and title in articles:
            parts.append((title, articles[title]))

    if not parts:
        return ""

    if max_chars > 0:
        overhead = sum(len(f"=== {t} ===\n\n\n") for t, _ in parts)
        budget = max(max_chars - overhead, len(parts) * 200)
        per_article = budget // len(parts)
        return "\n\n".join(
            f"=== {title} ===\n{content[:per_article]}" for title, content in parts
        )

    return "\n\n".join(f"=== {title} ===\n{content}" for title, content in parts)


async def query_oracle_answer(
    question: str,
    oracle_context: str,
    semaphore: asyncio.Semaphore,
    llm: ChatOpenAI,
) -> tuple[str | None, str]:
    """Oracle mode: send question + gold Wikipedia articles directly to the LLM."""
    user_message = (
        f"Here are the relevant Wikipedia articles:\n\n{oracle_context}\n\n"
        f"Question: {question}\n\nAnswer the question based on the articles above. Be concise."
    )
    async with semaphore:
        try:
            response = await llm.ainvoke([
                {"role": "system", "content": _oracle_system_prompt},
                {"role": "user", "content": user_message},
            ])
            return response.content, oracle_context
        except Exception as e:
            logger.debug(f"Oracle LLM error for question {question!r}: {e}")
            return None, oracle_context


async def query_llm_answer(
    question: str,
    semaphore: asyncio.Semaphore,
    llm: ChatOpenAI,
) -> tuple[str | None, str]:
    """No-RAG mode: send the raw question directly to the LLM."""
    async with semaphore:
        try:
            response = await llm.ainvoke([{"role": "user", "content": question}])
            return response.content, ""
        except Exception as e:
            logger.debug(f"No-RAG LLM error for question {question!r}: {e}")
            return None, ""


# ─── LLM accuracy judge ─────────────────────────────────────────────────────

_accuracy_judge_system_prompt = """\
You are an impartial factual evaluator. You will be given a question, the \
expected correct answer (gold), whether the gold answer is unanswerable, and a generated answer.

Your task is to determine if the generated answer is **factually correct** \
with respect to the gold answer. The generated answer does NOT need to be \
word-for-word identical — it just needs to convey the same factual information.

Special rule for unanswerable questions:
- If the gold answer is unanswerable, then the generated answer is correct only if it clearly abstains \
  or states that the answer cannot be determined from the provided context.
- If the gold answer is unanswerable and the generated answer provides a specific factual answer, it is incorrect.

You MUST reply with ONLY a JSON object in this exact format (no other text):
{"correct": true/false, "justification": "<one sentence>"}
"""


class _AccuracyJudgeResponse(BaseModel):
    correct: bool = Field(..., description="Whether the generated answer is factually correct")
    justification: str = Field(..., description="One-sentence justification")


def _make_accuracy_judge() -> ChatOpenAI:
    return ChatOpenAI(
        model=JUDGE_MODEL,
        base_url=JUDGE_BASE_URL,
        api_key=JUDGE_API_KEY,
        temperature=0.0,
        max_tokens=256,
    ).with_structured_output(_AccuracyJudgeResponse)


async def accuracy_judge(
    question: str,
    gold_answer: str,
    generated_answer: str,
    is_unanswerable: bool,
    semaphore: asyncio.Semaphore,
    judge: ChatOpenAI,
) -> tuple[bool, str]:
    """Run the accuracy judge, returns (correct, justification)."""
    user_message = (
        f"Question:\n{question}\n\n"
        f"Gold answer:\n{gold_answer}\n\n"
        f"Gold is unanswerable:\n{is_unanswerable}\n\n"
        f"Generated answer:\n{generated_answer}"
    )
    async with semaphore:
        try:
            response = await judge.ainvoke([
                {"role": "system", "content": _accuracy_judge_system_prompt},
                {"role": "user", "content": user_message},
            ])
            return response.correct, response.justification
        except Exception as e:
            logger.debug(f"Accuracy judge error: {e}")
            return False, ""


async def run_accuracy_judging(results: list[dict], concurrency: int = 10) -> None:
    """Run the accuracy judge on all results with a generated answer."""
    judge_semaphore = asyncio.Semaphore(concurrency)
    acc_judge = _make_accuracy_judge()

    to_judge = [(i, r) for i, r in enumerate(results) if r["generated_answer"]]
    acc_tasks = [
        accuracy_judge(
            r["question"],
            r["gold_answer"],
            r["generated_answer"],
            r.get("is_unanswerable", False),
            judge_semaphore,
            acc_judge,
        )
        for _, r in to_judge
    ]
    acc_scores = await tqdm.gather(*acc_tasks, desc="Accuracy judge")

    for r in results:
        r.setdefault("accuracy", False)
        r.setdefault("accuracy_justification", "")
    for (i, _), (correct, justification) in zip(to_judge, acc_scores):
        results[i]["accuracy"] = correct
        results[i]["accuracy_justification"] = justification


# ─── Result helpers ──────────────────────────────────────────────────────────


def _build_result(row, generated_answer, sources_content, mode, **extra) -> dict:
    """Build a single result entry."""
    expected_exact_match_answers = row.get("expected_exact_match_answers")
    if not expected_exact_match_answers:
        expected_exact_match_answers = [row["Answer"]]
    entry = {
        "question": row["Prompt"],
        "gold_answer": row["Answer"],
        "expected_exact_match_answers": expected_exact_match_answers,
        "generated_answer": generated_answer or "",
        "sources_content": sources_content,
        "reasoning_types": row.get("reasoning_types", ""),
        "dataset_name": row.get("dataset_name", "FRAMES"),
        "is_unanswerable": row.get("is_unanswerable", False),
        "mode": mode,
    }
    entry.update(extra)
    return entry


def _normalize_exact_match_text(text: str) -> str:
    text = text.casefold().strip()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = " ".join(text.split())
    return text


def _compute_exact_match(generated_answer: str, expected_answers: list[str]) -> bool:
    generated_norm = _normalize_exact_match_text(generated_answer)
    if not generated_norm:
        return False
    return any(generated_norm == _normalize_exact_match_text(answer) for answer in expected_answers if answer)


def annotate_exact_match(results: list[dict]) -> None:
    for result in results:
        expected = result.get("expected_exact_match_answers", [result.get("gold_answer", "")])
        result["exact_match"] = _compute_exact_match(result.get("generated_answer", ""), expected)


def _print_summary(results: list[dict], total_questions: int) -> None:
    """Print accuracy summary and breakdown by reasoning type."""
    valid = [r for r in results if r["generated_answer"]]
    mode = results[0].get("mode", "rag") if results else "rag"
    mode_display = {
        "oracle": "ORACLE", "no_rag": "NO_RAG",
        "gold_workspaces": "GOLD_WORKSPACES", "rag": "RAG",
    }.get(mode, mode.upper())

    print(f"\n{'='*60}")
    print(f"  Mode: {mode_display}")
    print(f"{'='*60}")

    acc_results = [r for r in valid if "accuracy" in r]
    if acc_results:
        correct = sum(1 for r in acc_results if r["accuracy"])
        print(f"  Accuracy: {correct / len(acc_results):.1%} ({correct}/{len(acc_results)})")
    else:
        print("  Accuracy: N/A")

    em_results = [r for r in valid if "exact_match" in r]
    if em_results:
        exact = sum(1 for r in em_results if r["exact_match"])
        print(f"  Exact match: {exact / len(em_results):.1%} ({exact}/{len(em_results)})")
        ua = [r for r in em_results if r.get("is_unanswerable")]
        if ua:
            ua_exact = sum(1 for r in ua if r["exact_match"])
            print(f"  EM unanswerable: {ua_exact / len(ua):.1%} ({ua_exact}/{len(ua)})")
    else:
        print("  Exact match: N/A")

    print(f"  Total questions: {total_questions} | Valid answers: {len(valid)}")
    print(f"{'='*60}")

    type_groups: dict[str, list[dict]] = defaultdict(list)
    for r in valid:
        rtype = r.get("reasoning_types", "unknown") or "unknown"
        type_groups[rtype].append(r)

    if len(type_groups) > 1 or (len(type_groups) == 1 and "unknown" not in type_groups):
        print(f"\n{'TYPE':<35} {'COUNT':>6} {'ACCURACY':>10}")
        print(f"{'-'*55}")
        for rtype in sorted(type_groups.keys()):
            group = type_groups[rtype]
            acc_group = [r for r in group if "accuracy" in r]
            acc = sum(1 for r in acc_group if r["accuracy"]) / len(acc_group) if acc_group else 0
            print(f"{rtype:<35} {len(group):>6} {acc:>9.1%}")
        print(f"{'='*55}")


# ─── Main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate OpenRAG on the FRAMES benchmark.")
    parser.add_argument("--partition", default="FRAMES")
    parser.add_argument("--output", default="results_frames.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-rag", action="store_true", default=False,
                        help="Bypass OpenRAG, send question directly to LLM")
    parser.add_argument("--oracle", action="store_true", default=False,
                        help="Provide gold Wikipedia articles directly to LLM (upper bound)")
    parser.add_argument("--gold-workspaces", action="store_true", default=False,
                        help="Create one workspace per question with only its gold files")
    parser.add_argument("--gold-workspace-prefix", default=None,
                        help="Prefix for per-question gold workspaces (default: <partition>-goldws)")
    parser.add_argument("--reuse-gold-workspaces", action="store_true", default=False,
                        help="Reuse existing gold workspaces, skip creation/file attachment")
    parser.add_argument("--max-context-chars", type=int, default=20000,
                        help="Max chars for oracle context (default: 20000). 0 = no limit.")
    parser.add_argument("--docs-dir", default="./frames_docs",
                        help="Directory with .md Wikipedia articles for oracle mode (default: ./frames_docs)")
    parser.add_argument("--judge-concurrency", type=int, default=10)
    parser.add_argument("--from-results", metavar="FILE", default=None,
                        help="Load existing results JSON, skip generation, recompute metrics")
    parser.add_argument("--dataset-path", default=None,
                        help="Dataset JSON path relative to automatic-evaluation-pipeline/ or absolute")
    args = parser.parse_args()

    # Mutual exclusivity
    exclusive_modes = [name for name, flag in [
        ("--no-rag", args.no_rag), ("--oracle", args.oracle), ("--gold-workspaces", args.gold_workspaces),
    ] if flag]
    if len(exclusive_modes) > 1:
        parser.error(f"{' and '.join(exclusive_modes)} are mutually exclusive")
    if args.reuse_gold_workspaces and not args.gold_workspaces:
        parser.error("--reuse-gold-workspaces requires --gold-workspaces")

    # Every mode ends by running the LLM judge, so its config must be present.
    _require_llm_config(judge_only=True)

    dataset = load_dataset_cached(limit=args.limit, dataset_path=args.dataset_path)
    docs_dir = Path(args.docs_dir)
    if not docs_dir.is_absolute():
        docs_dir = (Path(__file__).parent / docs_dir).resolve()

    # ── Load existing results or generate ────────────────────────────────────
    if args.from_results:
        from_path = Path(args.from_results) if Path(args.from_results).is_absolute() else Path(__file__).parent / args.from_results
        print(f"Loading existing results from {from_path} ...")
        with open(from_path, encoding="utf-8") as f:
            results: list[dict] = json.load(f)
        annotate_exact_match(results)
        await run_accuracy_judging(results, concurrency=args.judge_concurrency)

    elif args.no_rag:
        _require_llm_config()
        print(f"Evaluating {len(dataset)} questions [NO_RAG]")
        llm = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0.2, max_tokens=512)
        sem = asyncio.Semaphore(args.concurrency)
        raw = await tqdm.gather(
            *[query_llm_answer(row["Prompt"], sem, llm) for row in dataset],
            desc="Querying LLM (no RAG)",
        )
        results = [_build_result(row, ans, src, "no_rag") for row, (ans, src) in zip(dataset, raw)]
        annotate_exact_match(results)
        await run_accuracy_judging(results, concurrency=args.judge_concurrency)

    elif args.oracle:
        _require_llm_config()
        print(f"Evaluating {len(dataset)} questions [ORACLE]")
        if not docs_dir.exists():
            print(f"ERROR: docs dir {docs_dir} not found. Run setup_frames.py first.")
            return
        oracle_articles = load_articles_from_disk(dataset, docs_dir)
        llm = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0.2, max_tokens=512)
        sem = asyncio.Semaphore(args.concurrency)
        raw = await tqdm.gather(
            *[
                query_oracle_answer(
                    row["Prompt"],
                    build_oracle_context(
                        row,
                        oracle_articles,
                        max_chars=args.max_context_chars,
                    ),
                    sem, llm,
                )
                for row in dataset
            ],
            desc="Querying LLM (oracle)",
        )
        results = [_build_result(row, ans, src, "oracle") for row, (ans, src) in zip(dataset, raw)]
        annotate_exact_match(results)
        await run_accuracy_judging(results, concurrency=args.judge_concurrency)

    elif args.gold_workspaces:
        workspace_prefix = args.gold_workspace_prefix or f"{args.partition}-goldws"
        reuse = args.reuse_gold_workspaces
        print(f"Evaluating {len(dataset)} questions [GOLD_WORKSPACES{'_REUSE' if reuse else ''}, partition={args.partition}]")

        async with httpx.AsyncClient(timeout=600) as client:
            if not await _check_openrag_health(client):
                return
            available = await get_available_openrag_partitions(client)
            if args.partition not in available:
                print(f"ERROR: partition '{args.partition}' not available. Available: {', '.join(sorted(available)) or '(none)'}")
                return

            if reuse:
                prepared = [
                    (_build_gold_name(workspace_prefix, i), _get_gold_file_ids(row))
                    for i, row in enumerate(dataset)
                ]
                existing_ws = await get_available_workspaces(client, args.partition)
                missing = [ws for ws, _ in prepared if ws not in existing_ws]
                if missing:
                    print(f"ERROR: {len(missing)} gold workspaces missing: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
                    return
            else:
                ws_sem = asyncio.Semaphore(args.concurrency)

                async def _prep(i, row):
                    async with ws_sem:
                        return await prepare_gold_workspace_for_question(client, i, row, args.partition, workspace_prefix)

                prepared = await tqdm.gather(
                    *[_prep(i, row) for i, row in enumerate(dataset)],
                    desc="Preparing gold workspaces",
                )

        sem = asyncio.Semaphore(args.concurrency)
        raw = await tqdm.gather(
            *[query_openrag_answer(row["Prompt"], args.partition, sem, workspace=ws) for row, (ws, _) in zip(dataset, prepared)],
            desc="Querying OpenRAG (gold workspaces)",
        )
        results = [
            _build_result(row, ans, src, "gold_workspaces", workspace_id=ws, question_index=i, gold_file_ids=fids)
            for i, (row, (ws, fids), (ans, src)) in enumerate(zip(dataset, prepared, raw))
        ]
        annotate_exact_match(results)
        await run_accuracy_judging(results, concurrency=args.judge_concurrency)

    else:
        print(f"Evaluating {len(dataset)} questions on partition '{args.partition}' at {OPENRAG_BASE_URL}")
        async with httpx.AsyncClient(timeout=60) as client:
            if not await _check_openrag_health(client):
                return
        sem = asyncio.Semaphore(args.concurrency)
        raw = await tqdm.gather(
            *[query_openrag_answer(row["Prompt"], args.partition, sem) for row in dataset],
            desc="Querying OpenRAG",
        )
        results = [_build_result(row, ans, src, "rag") for row, (ans, src) in zip(dataset, raw)]
        annotate_exact_match(results)
        await run_accuracy_judging(results, concurrency=args.judge_concurrency)

    # ── Save & summary ───────────────────────────────────────────────────────
    output_path = Path(__file__).parent / args.output
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to {output_path}")
    _print_summary(results, len(dataset))


if __name__ == "__main__":
    asyncio.run(main())
