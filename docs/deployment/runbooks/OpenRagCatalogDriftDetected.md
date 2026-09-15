# OpenRagCatalogDriftDetected

**Severity:** warning · **Fires after:** 15 min

```
sum(rate(openrag_retrieval_orphan_chunks_dropped_total[15m])) > 0
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

## First checks

The metric is aggregate by design. The affected `partition` and `file_id` are **log**
fields — with `LOG_FORMAT=json`, filter the collector for the drop event.

## Actions

Re-run deletion for the orphaned file ids so the vector store matches the catalog. If the
cause was a restore, expect drift proportional to the gap between the two backups, and
reconcile rather than chasing individual files.

## Verify recovery

The rate returns to zero and stays there for a full 15-minute window.
