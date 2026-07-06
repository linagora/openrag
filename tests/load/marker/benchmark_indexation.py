"""A/B indexation benchmark — time a batch of PDFs through the running stack.

Deployment-agnostic: point it at the **on-Ray** stack, then the **off-Ray**
(marker-serve) stack — same PDFs, same partition config — and compare. It
measures wall-clock from the first upload to the last task reaching a terminal
state, plus throughput (files/min and pages/min).

It is black-box (only the public HTTP API), so it captures each architecture's
*real* end-to-end cost — including the off-Ray MinIO+NATS handoff — which is
exactly what you want to compare.

FAIRNESS — run BOTH deployments identically (mirrors marker_page_chunking.md):
  * same PDF set, same `MARKER_CHUNK_SIZE` and `MARKER_MAX_PROCESSES`
  * on Ray, set `RAY_...`/`marker_pool_size=1` so one Ray worker matches the one
    marker-serve worker (otherwise Ray has more parallelism → not apples-to-apples)
  * isolate parsing from downstream noise:
        CONTEXTUAL_RETRIEVAL=false
        IMAGE_CAPTIONING=false
        VDB_ENABLE_INSERTION=false
  * always `--warmup` first: the first parse pays a one-off model-load cost
    (marker-serve loads on boot; Ray loads on first task) — don't let it skew the
    timed run.
  * measure GPU separately on the L4 host, alongside the run:
        nvidia-smi --query-gpu=memory.used --format=csv -l 1

Usage (self-contained uv script — installs only httpx + pypdfium2 in an isolated
env, NOT the full project venv):
    uv run tests/load/marker/benchmark_indexation.py \
        --base-url http://localhost:8080 --token "$AUTH_TOKEN" \
        --partition bench --pdf-dir ./pdfs --label on-ray --warmup

Then redeploy off-Ray and run again with --label off-ray against the same PDFs.
"""

# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "pypdfium2"]
# ///

from __future__ import annotations

import argparse
import asyncio
import re
import time
import uuid
from pathlib import Path

import httpx


def _safe_id(name: str) -> str:
    """File ids only allow [A-Za-z0-9._:-]; filenames often have spaces/accents."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", name)

# Terminal task states across both pipelines (COMPLETED/CANCELLED per the state
# machine; SUCCESS accepted in case an older build reports it).
_SUCCESS = {"COMPLETED", "SUCCESS"}
_TERMINAL = _SUCCESS | {"FAILED", "CANCELLED"}


def _page_count(path: Path) -> int:
    try:
        import pypdfium2

        pdf = pypdfium2.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        return 0


async def _upload(client: httpx.AsyncClient, base: str, partition: str, path: Path, run_tag: str, index: int) -> str | None:
    # Sanitized + index-prefixed: valid per the file-id whitelist AND unique per run
    # (so re-running against the same partition never 409s, even on name collisions).
    file_id = f"{run_tag}-{index}-{_safe_id(path.name)}"
    with open(path, "rb") as f:
        resp = await client.post(
            f"{base}/indexer/partition/{partition}/file/{file_id}",
            files={"file": (path.name, f.read(), "application/pdf")},
        )
    if resp.status_code >= 300:
        print(f"  ! upload failed for {path.name}: {resp.status_code} {resp.text[:120]}")
        return None
    # Response gives a task_status_url ending in the task id.
    return resp.json()["task_status_url"].rstrip("/").split("/")[-1]


async def _state(client: httpx.AsyncClient, base: str, task_id: str) -> str | None:
    resp = await client.get(f"{base}/indexer/task/{task_id}")
    if resp.status_code == 404:
        return None
    return resp.json().get("task_state")


async def _await_terminal(client: httpx.AsyncClient, base: str, task_id: str, poll: float) -> str:
    while True:
        state = await _state(client, base, task_id)
        if state in _TERMINAL:
            return state
        await asyncio.sleep(poll)


async def _run_batch(client, base, partition, pdfs, poll, upload_conc, run_tag):
    sem = asyncio.Semaphore(upload_conc)

    async def upload_one(index, p):
        async with sem:
            return p, await _upload(client, base, partition, p, run_tag, index)

    t0 = time.monotonic()
    uploaded = await asyncio.gather(*(upload_one(i, p) for i, p in enumerate(pdfs)))
    t_uploaded = time.monotonic()

    task_ids = {tid: p for p, tid in uploaded if tid}
    states = await asyncio.gather(*(_await_terminal(client, base, tid, poll) for tid in task_ids))
    t_done = time.monotonic()
    return t0, t_uploaded, t_done, list(task_ids.values()), states


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. http://localhost:8080")
    ap.add_argument("--token", default=None, help="Bearer token (AUTH_TOKEN); omit if auth is disabled")
    ap.add_argument("--partition", required=True)
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--label", default="run", help="tag for this run, e.g. on-ray / off-ray")
    ap.add_argument("--poll", type=float, default=1.0, help="task poll interval (s)")
    ap.add_argument("--upload-concurrency", type=int, default=16)
    ap.add_argument("--warmup", action="store_true", help="parse one file first to absorb model-load cost")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    pdfs = sorted(p for p in Path(args.pdf_dir).glob("**/*") if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        raise SystemExit(f"no PDFs under {args.pdf_dir}")

    total_pages = sum(_page_count(p) for p in pdfs)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        hc = await client.get(f"{base}/health_check")
        hc.raise_for_status()

        if args.warmup:
            print(f"[{args.label}] warmup: parsing 1 file to absorb model load…")
            wt = f"warmup-{uuid.uuid4().hex[:8]}"
            _, _, _, _, _ = await _run_batch(client, base, args.partition, pdfs[:1], args.poll, 1, wt)

        run_tag = f"{args.label}-{uuid.uuid4().hex[:8]}"
        print(f"[{args.label}] timing {len(pdfs)} PDFs ({total_pages} pages)…")
        t0, t_up, t_done, files, states = await _run_batch(
            client, base, args.partition, pdfs, args.poll, args.upload_concurrency, run_tag
        )

    ok = sum(1 for s in states if s in _SUCCESS)
    failed = len(states) - ok
    total = t_done - t0
    upload_s = t_up - t0
    parse_s = t_done - t_up
    fpm = len(files) / total * 60 if total else 0
    ppm = total_pages / total * 60 if total and total_pages else 0

    print("\n" + "=" * 52)
    print(f" RESULT [{args.label}]")
    print("=" * 52)
    print(f"  files                {len(files)}  (ok={ok}, failed={failed})")
    print(f"  pages                {total_pages}")
    print(f"  upload phase         {upload_s:8.1f} s")
    print(f"  parse/drain phase    {parse_s:8.1f} s")
    print(f"  TOTAL wall-clock     {total:8.1f} s  ({total / 60:.1f} min)")
    print(f"  throughput           {fpm:8.1f} files/min")
    if ppm:
        print(f"                       {ppm:8.1f} pages/min")
    print("=" * 52)
    print("Paste into your on-ray vs off-ray comparison table.")


if __name__ == "__main__":
    asyncio.run(main())
