# OpenRagCatalogDriftDetected

**Severity:** critical · **Fires after:** ~11 min from a single dropped chunk, by default

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `for.OpenRagCatalogDriftDetected` (default 5m); the `> 0` test is semantic and not tunable.
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule <release>-alerts -o yaml
> ```

```
sum(increase(openrag_retrieval_orphan_chunks_dropped_total[1h])) > 0
```

## What it means

Retrieval is returning chunks from the vector store for files the **catalog does not know
about**, and dropping them. The two stores disagree: Milvus still holds vectors for a
file Postgres no longer lists.

Retrieval still answers, so nothing looks broken from outside — it is quietly returning
fewer results than it should, and the dropped ones may have been the best matches.

## Read the threshold correctly

Only **non-zero** is meaningful. The counter's own definition notes that repeated
retrievals can count the same chunk again, so the rate is not a document count and a
higher rate does not mean more affected files — it may mean one popular orphan. Never set
a magnitude threshold on it.

## Likely causes

1. **A delete that half-succeeded.** The catalog row went and the vectors stayed, or a
   partition delete did not cascade into Milvus.
2. **An interrupted re-index.** The idempotency delete ran, then the insert failed.
3. **A restore from a backup taken at a different moment for Postgres and Milvus.** These
   are two stores with independent backup timelines; a point-in-time mismatch shows up
   exactly like this.

## How big is it? The metric cannot tell you — the logs can

This is the triage step, because the two shapes need different responses and the counter
looks identical for both:

- **A few stale files, queried repeatedly.** The counter climbs steadily because the same
  orphan is re-retrieved, not because more files are affected.
- **An entire partition orphaned** — a partition deleted in Postgres whose Milvus
  partition was never dropped, or a restore where the two stores came from different
  moments. Every query touching it drops everything.

`_warn_orphans` (`services/storage/catalog_searcher.py`) logs the distinguishing signal:

| Log field | What it tells you |
| --- | --- |
| `dropped_chunks` | Chunks dropped in this retrieval |
| `dropped_files` | **Distinct files** behind them — this is the number that separates the two cases |
| `orphaned_files_sample` | Up to 10 `{partition, file_id}` pairs, truncated to 128 chars |

Two properties of that log line to plan around: it is **rate-limited to once per minute**
process-wide, and the sample is capped at 10 files. So it tells you the shape of the
problem and gives you a foothold, but it is not an inventory — do not try to enumerate
the affected files from the logs. Query the two stores directly once you know which
partition is involved.

With `LOG_FORMAT=json`, filter the collector for `dropped_files`.

## What this alert is not

**Not a catalog outage.** The lookup runs through a plain `asyncpg` pool with no fallback
and no cache — deliberately, so a catalog outage can never expose deleted files. A
connection failure therefore *raises* and the retrieval query fails outright; it does not
silently drop every chunk. If this counter is climbing, the catalog answered successfully
and genuinely does not list those files.

## Actions

Re-run deletion for the orphaned file ids so the vector store matches the catalog. If the
cause was a restore, expect drift proportional to the gap between the two backups, and
reconcile rather than chasing individual files.

## Expect it to keep firing while you work

The threshold is `> 0`, and the condition persists until the two stores are reconciled —
so unlike a rate or a saturation alert, this one does not clear on its own and cannot be
made to stop quickly. At `critical` that means it will keep paging.

That is deliberate: the alert exists because the damage is otherwise invisible, and
silencing it is a decision someone should have to make explicitly. Silence it for a
bounded window while reconciling, rather than downgrading it.

## Resolving is not the same as fixed

**This alert clearing tells you nothing about whether the drift is gone.**

The counter only moves when a query happens to touch an orphaned file. The rule therefore
sees drops, never the underlying disagreement between the stores — so it resolves when
*no drop has been observed for an hour*, which happens both when you have reconciled the
data and when nobody has queried the affected files lately. The orphans are still there in
the second case, waiting for the next query that touches them.

The hour-long window exists to make that mistake less likely: a shorter one resolved
fifteen minutes after the last query and re-fired the next time someone searched, which
reads as a flapping alert rather than an unfixed problem. It does not eliminate the
mistake.

Treat this as resolved only once you have confirmed the two stores agree for the affected
files. Reconcile first, then let it clear — never the other way round.

## Verify recovery

No further drops are observed for a full hour. See the caveat above: that is
evidence of absence only if you have already reconciled the stores.
