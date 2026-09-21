---
title: OpenRagCanaryFailing
description: Runbook for the synthetic canary alert.
---

The [synthetic canary](/openrag/documentation/synthetic_canary/) indexes,
retrieves and deletes a known document on a schedule. This alert means that
this path, which every user's document follows, is not working. It has two
conditions, told apart by the `condition` label:

- **`runs-failing`**: two runs in a row failed. The product is broken for
  users, or for new documents at least.
- **`not-running`**: the canary is enabled but no run has finished for over
  two intervals. Either no replica can run it or it stopped. Nothing is known
  about the product itself.

## First look

1. Find the failure. Each failed run logs one line on the API that ran it:

   ```bash
   # Docker Compose
   docker compose logs openrag | grep "Canary run"
   # Kubernetes
   kubectl logs deploy/<release>-openrag | grep "Canary run"
   ```

   The line carries `failed_stage`, `reason` and the time spent in each stage
   (`stage_seconds`).

2. Or ask Prometheus which stage fails:

   ```promql
   increase(openrag_canary_failures_total[1h])
   ```

3. Check the dependencies the product needs: `GET /ready` reports Postgres,
   Milvus, Ray and the model endpoints.

## By failed stage

**`setup`**: the canary could not prepare its user or partition, or could not
remove an earlier run's document.

- `partition 'openrag-canary' is owned by user(s) [...]`: that partition
  belongs to someone else, which can happen on a deployment older than the
  name's reservation. Rename or delete it; the canary creates its own on the
  next run.
- A Postgres or `MODEL_ENDPOINT_NOT_FOUND` error: the catalog is unreachable,
  or no default embedder is configured.

**`queue`**: no worker picked the indexing task up within
`CANARY_INDEX_TIMEOUT_SECONDS`, or submitting it failed.

- Look at the queue (`GET /queue/info` or the admin UI's jobs page). A bulk
  import can hold every worker for longer than the timeout. Users uploading
  now wait just as long, so the alert is accurate. It clears once the backlog
  drains.
- An empty queue with this error points at the indexer pool: see
  `GET /actors/` and the Ray dashboard for dead or restarting actors.

**`index`**: the task failed or did not finish in time. The reason includes
the task's error. The usual causes:

- an embedder endpoint that is down, or that returns vectors whose dimension
  does not match the collection (typically after the default embedder changed);
- Milvus refusing the insert.

**`query`**: indexing succeeded but retrieval did not return the document.

- `retrieval returned 0 chunk(s)`: the collection is not loaded, the query
  embedder differs from the one that indexed, or a similarity threshold in the
  default retrieval preset filters everything out.
- An exception names the reranker or the Milvus search that failed.

**`cleanup`**: the delete failed, or data survived it. `catalog row ... survived`
or `chunk(s) ... survived` means deletions leave documents behind, and so
deleted documents may still be searchable. Run the
[catalog reconciliation](/openrag/documentation/catalog_reconciliation/) on
`openrag-canary` and on a user partition.

## Not running

- `openrag_canary_leader` should be `1` on exactly one replica. If it is `0`
  everywhere, no replica can take the lease: the API log shows
  `Canary lease unavailable`, and Postgres is unreachable.
- `openrag_canary_enabled` is `1` but the API log shows
  `Synthetic canary enabled but the service container is unavailable`: the API
  started degraded. Fix the startup error it logged first.
- After an API restart, the first run starts after
  `CANARY_INITIAL_DELAY_SECONDS`. The alert waits 15 minutes before firing, so
  a restart alone should not trigger it.

## After the fix

The `runs-failing` alert clears at the next passing run. To avoid waiting up
to an interval, restart the replica that holds the lease: it runs one minute
after startup. A leftover `canary-*` document from an interrupted run is
removed automatically by a later run.
