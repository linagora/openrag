"""
Download FRAMES benchmark Wikipedia articles and index them into OpenRAG.

pip install dataset is needed if you don't want to manually download the dataset JSON from HuggingFace.

The FRAMES benchmark (google/frames-benchmark) contains 824 multi-hop questions
referencing 2-15 Wikipedia articles each (2474 articles). This script:
  1. Loads the dataset (cached JSON or HuggingFace)
  2. Extracts every unique Wikipedia URL
  3. Fetches each article in the chosen format (md / html / pdf) (45 minutes with default concurrency = 3 API recommend <=5)
  4. Creates the target partition and indexes the files

Usage:
    cd automatic-evaluation-pipeline
    python setup_frames.py [--partition FRAMES] [--limit N]      # pdf (default)
    python setup_frames.py --format md                           # plain-text markdown
    python setup_frames.py --format html                         # HTML (requires OpenRAG with text/html loader)
    python setup_frames.py --index-only                          # skip download, index existing files
    python setup_frames.py --retry-missing                       # fetch only missing articles

Environment variables (from .env):
    APP_URL, APP_PORT, AUTH_TOKEN
"""

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx
from dotenv import load_dotenv
from loguru import logger
from tqdm.asyncio import tqdm

load_dotenv()

# ─── Env / config ────────────────────────────────────────────────────────────

APP_URL = os.environ.get("APP_URL", "localhost")
APP_PORT = os.environ.get("APP_PORT", "8080")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "sk-1234")

OPENRAG_BASE_URL = f"http://{APP_URL}:{APP_PORT}"

WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_REST_HTML = "https://en.wikipedia.org/api/rest_v1/page/html"
WIKIPEDIA_REST_PDF = "https://en.wikipedia.org/api/rest_v1/page/pdf"
WIKIPEDIA_USER_AGENT = "OpenRAG-FRAMES-Benchmark/1.0 (https://github.com/linagora/openrag; eval pipeline)"

TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
POLL_INTERVAL = 5
# Upper bound on how long to poll a single indexing task before giving up, so a
# stuck or silently-dropped task can never hang the whole run forever.
MAX_POLL_SECONDS = 1800

DATASET_CACHE = Path(__file__).parent / "frames_dataset.json"

FORMAT_DEFAULTS: dict[str, tuple[str, str]] = {
    "md": ("./frames_docs", "text/markdown"),
    "pdf": ("./frames_pdf", "application/pdf"),
    "html": ("./frames_html", "text/html"),
}


# ─── Dataset ─────────────────────────────────────────────────────────────────


def load_dataset_cached(limit: int | None = None) -> list[dict]:
    """Load dataset from local cache, downloading from HuggingFace when the cache is absent."""
    if DATASET_CACHE.exists():
        logger.info(f"Loading dataset from cache ({DATASET_CACHE.name})...")
        with open(DATASET_CACHE, encoding="utf-8") as f:
            dataset = json.load(f)
    else:
        logger.info("Downloading FRAMES benchmark from HuggingFace...")
        from datasets import load_dataset
        hf_dataset = load_dataset("google/frames-benchmark", split="test")
        dataset = [dict(row) for row in hf_dataset]
        with open(DATASET_CACHE, "w", encoding="utf-8") as f:
            json.dump(dataset, f, ensure_ascii=False, indent=2)
        logger.info(f"Cached {len(dataset)} questions to {DATASET_CACHE.name}")

    if limit is not None:
        dataset = dataset[:limit]
    logger.info(f"Loaded {len(dataset)} questions.")
    return dataset


# ─── Wikipedia helpers ───────────────────────────────────────────────────────


def extract_title_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if "wikipedia.org" not in parsed.netloc:
        return None
    match = re.match(r"/wiki/(.+)", parsed.path)
    if match:
        return unquote(match.group(1)).replace("_", " ")
    return None


def parse_wiki_links(row: dict) -> list[str]:
    """Extract Wikipedia URLs from a FRAMES dataset row.

    Handles rows where several URLs are glued into a single string.
    """
    wiki_links = row.get("wiki_links")
    if not wiki_links:
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


def extract_all_titles(dataset: list[dict]) -> list[str]:
    """Extract unique Wikipedia article titles referenced by the dataset."""
    titles: set[str] = set()
    for row in dataset:
        for url in parse_wiki_links(row):
            title = extract_title_from_url(url)
            if title:
                titles.add(title)
    return sorted(titles)


def safe_filename(title: str) -> str:
    safe = re.sub(r'[^\w\s\-()]', '', title).strip()
    safe = re.sub(r'\s+', '_', safe)
    if not safe:
        # Stable md5 name so the same title always maps to the same file.
        safe = f"article_{hashlib.md5(title.encode('utf-8')).hexdigest()[:8]}"
    return safe


def title_to_wiki_slug(title: str) -> str:
    decoded = unquote(title)
    return quote(decoded.replace(" ", "_"), safe="/:@!$&'()*+,;=-._~")


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait, honoring a numeric or HTTP-date Retry-After header.

    Falls back to capped exponential backoff when the header is absent or
    unparseable.
    """
    header = resp.headers.get("retry-after")
    fallback = float(min(2 ** attempt + 1, 60))
    if not header:
        return fallback
    try:
        return float(header)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(header)
        now = datetime.now(UTC) if retry_at.tzinfo else datetime.now()
        return max((retry_at - now).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return fallback


# ─── Fetch ───────────────────────────────────────────────────────────────────


async def fetch_markdown(
    client: httpx.AsyncClient, title: str, max_retries: int = 5,
) -> tuple[str, bytes | None]:
    """Fetch an article as plain-text markdown via the `prop=extracts` API."""
    params = {
        "action": "query", "titles": title, "prop": "extracts",
        "explaintext": "true", "format": "json",
    }
    for attempt in range(max_retries):
        try:
            resp = await client.get(WIKIPEDIA_API_URL, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after_seconds(resp, attempt)
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait)
                    continue
                return title, None
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page_id, page in pages.items():
                if page_id == "-1":
                    logger.warning(f"Wikipedia article not found: {title}")
                    return title, None
                extract = page.get("extract", "")
                if extract:
                    md = f"# {title}\n\n{extract}"
                    return title, md.encode("utf-8")
            return title, None
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2 ** attempt + 1, 60))
                continue
            return title, None
        except Exception as e:
            logger.debug(f"Failed to fetch '{title}': {e}")
            return title, None
    return title, None


async def fetch_rest(
    client: httpx.AsyncClient, title: str, fmt: str, max_retries: int = 5,
) -> tuple[str, bytes | None]:
    """Fetch an article as HTML or PDF via the Wikipedia REST API."""
    base = WIKIPEDIA_REST_HTML if fmt == "html" else WIKIPEDIA_REST_PDF
    url = f"{base}/{title_to_wiki_slug(title)}"
    for attempt in range(max_retries):
        try:
            resp = await client.get(url)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after_seconds(resp, attempt)
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait)
                    continue
                return title, None
            if resp.status_code == 404:
                logger.warning(f"Wikipedia article not found: {title}")
                return title, None
            resp.raise_for_status()
            return title, resp.content
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2 ** attempt + 1, 60))
                continue
            return title, None
        except Exception as e:
            logger.debug(f"Failed to fetch '{title}': {e}")
            return title, None
    return title, None


async def download_articles(
    titles: list[str], output_dir: Path, fmt: str, concurrency: int,
) -> list[tuple[str, Path]]:
    """Download articles for `titles` into `output_dir`, return (file_id, path) list."""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_stems = {p.stem for p in output_dir.glob(f"*.{fmt}")}

    ready: list[tuple[str, Path]] = []
    to_fetch: list[str] = []
    for title in titles:
        stem = safe_filename(title)
        if stem in existing_stems:
            ready.append((stem, output_dir / f"{stem}.{fmt}"))
        else:
            to_fetch.append(title)

    logger.info(f"Articles ({fmt}): {len(ready)} on disk, {len(to_fetch)} to fetch.")
    if not to_fetch:
        return ready

    sem = asyncio.Semaphore(concurrency)

    async def _fetch_with_sem(title: str, client: httpx.AsyncClient):
        async with sem:
            if fmt == "md":
                return await fetch_markdown(client, title)
            return await fetch_rest(client, title, fmt)

    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True,
        headers={"User-Agent": WIKIPEDIA_USER_AGENT},
    ) as client:
        tasks = [_fetch_with_sem(t, client) for t in to_fetch]
        results = await tqdm.gather(*tasks, desc=f"Fetching Wikipedia {fmt.upper()}")

    fetched = 0
    for title, content in results:
        if content is None:
            continue
        stem = safe_filename(title)
        path = output_dir / f"{stem}.{fmt}"
        path.write_bytes(content)
        ready.append((stem, path))
        fetched += 1
    logger.info(f"Fetched {fetched} new articles ({len(to_fetch) - fetched} failed).")
    return ready


# ─── OpenRAG upload ──────────────────────────────────────────────────────────


async def check_health(client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get(f"{OPENRAG_BASE_URL}/health_check")
        resp.raise_for_status()
        logger.info("OpenRAG API is up.")
        return True
    except Exception as e:
        logger.error(f"Cannot reach OpenRAG at {OPENRAG_BASE_URL}: {e}")
        return False


async def create_partition(client: httpx.AsyncClient, partition: str) -> None:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    resp = await client.post(f"{OPENRAG_BASE_URL}/partition/{partition}", headers=headers)
    if resp.status_code == 201:
        logger.info(f"Partition '{partition}' created.")
    elif resp.status_code == 409:
        logger.info(f"Partition '{partition}' already exists.")
    else:
        resp.raise_for_status()


async def upload_and_track(
    client: httpx.AsyncClient,
    partition: str,
    file_id: str,
    file_path: Path,
    mime: str,
    sem: asyncio.Semaphore,
) -> dict:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    url = f"{OPENRAG_BASE_URL}/indexer/partition/{partition}/file/{file_id}"
    async with sem:
        try:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, mime), "metadata": (None, "")}
                resp = await client.post(url, files=files, headers=headers)

            if resp.status_code == 409:
                return {"file_id": file_id, "status": "skipped"}
            if resp.status_code != 201:
                logger.error(f"Upload failed for '{file_id}': {resp.status_code} - {resp.text}")
                resp.raise_for_status()

            task_url = resp.json().get("task_status_url")
            if not task_url:
                return {"file_id": file_id, "status": "ERROR", "error": "no task_status_url in response"}
            if task_url.startswith("/"):
                task_url = f"{OPENRAG_BASE_URL}{task_url}"

            loop = asyncio.get_event_loop()
            deadline = loop.time() + MAX_POLL_SECONDS
            while True:
                poll = await client.get(task_url, headers=headers)
                if poll.status_code == 200:
                    state = poll.json().get("task_state", "UNKNOWN")
                    if state in TERMINAL_STATES:
                        return {"file_id": file_id, "status": state}
                else:
                    logger.warning(f"Poll failed for '{file_id}': {poll.status_code}")
                if loop.time() >= deadline:
                    return {"file_id": file_id, "status": "ERROR", "error": f"timed out after {MAX_POLL_SECONDS}s"}
                await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Error processing '{file_id}': {e}")
            return {"file_id": file_id, "status": "ERROR", "error": str(e)}


async def index_files(
    doc_files: list[tuple[str, Path]],
    partition: str,
    mime: str,
    concurrency: int,
) -> None:
    async with httpx.AsyncClient(timeout=600) as client:
        if not await check_health(client):
            return
        await create_partition(client, partition)

        logger.info(f"Uploading {len(doc_files)} files to '{partition}' (concurrency={concurrency})...")
        sem = asyncio.Semaphore(concurrency)
        tasks = [upload_and_track(client, partition, fid, path, mime, sem) for fid, path in doc_files]
        results = await tqdm.gather(*tasks, desc="Uploading & indexing")

    completed = sum(1 for r in results if r["status"] == "COMPLETED")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "ERROR")

    logger.info(f"\n{'='*60}")
    logger.info(f"Indexing complete for partition '{partition}'")
    logger.info(f"  COMPLETED: {completed}")
    logger.info(f"  FAILED:    {failed}")
    logger.info(f"  SKIPPED:   {skipped} (already existed)")
    logger.info(f"  ERRORS:    {errors}")
    logger.info(f"{'='*60}")

    if failed or errors:
        logger.info("\nFailed/errored files:")
        for r in results:
            if r["status"] in ("FAILED", "ERROR"):
                logger.info(f"  - {r['file_id']}: {r['status']} {r.get('error', '')}")


# ─── Main ────────────────────────────────────────────────────────────────────


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return ivalue


async def main() -> None:
    parser = argparse.ArgumentParser(description="Download + index FRAMES Wikipedia articles into OpenRAG.")
    parser.add_argument("--partition", default="FRAMES")
    parser.add_argument("--format", choices=["md", "pdf", "html"], default="pdf")
    parser.add_argument("--output-dir", default=None,
                        help="Override article directory (defaults per format).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=_positive_int, default=4,
                        help="Max concurrent uploads (default: 4)")
    parser.add_argument("--wiki-concurrency", type=_positive_int, default=3,
                        help="Max concurrent Wikipedia fetches (default: 3)")
    parser.add_argument("--index-only", action="store_true",
                        help="Skip download, index existing files from output-dir")
    parser.add_argument("--retry-missing", action="store_true",
                        help="Only fetch articles missing from output-dir, upload only the new ones")
    args = parser.parse_args()

    default_dir, mime = FORMAT_DEFAULTS[args.format]
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        Path(__file__).parent / default_dir
    ).resolve()

    if args.index_only:
        if not output_dir.exists():
            logger.error(f"Output directory {output_dir} does not exist.")
            return
        doc_files = [(p.stem, p) for p in sorted(output_dir.glob(f"*.{args.format}"))]
        logger.info(f"Index-only: {len(doc_files)} .{args.format} files in {output_dir}")
    else:
        dataset = load_dataset_cached(limit=args.limit)
        titles = extract_all_titles(dataset)
        logger.info(f"Found {len(titles)} unique Wikipedia articles referenced.")

        before = {p.stem for p in output_dir.glob(f"*.{args.format}")} if output_dir.exists() else set()
        doc_files = await download_articles(titles, output_dir, args.format, args.wiki_concurrency)
        if args.retry_missing:
            doc_files = [(fid, path) for fid, path in doc_files if fid not in before]
            logger.info(f"Will only upload {len(doc_files)} newly-fetched files.")

    if not doc_files:
        logger.info("Nothing to upload.")
        return

    await index_files(doc_files, args.partition, mime, args.concurrency)


if __name__ == "__main__":
    asyncio.run(main())
