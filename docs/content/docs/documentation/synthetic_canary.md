---
title: Synthetic canary
description: A known document indexed, retrieved and deleted on a schedule, so a broken search path is noticed even while every component reports itself healthy.
---

Readiness checks and metrics measure each component from the inside. All of
them can be green while the product is broken: an embedder swapped for one
with a different vector dimension, a Milvus collection that is not loaded,
retrieval quietly returning nothing. The synthetic canary follows the path a
user's document takes, from upload to answer to deletion, and reports whether
the document came out the other end.

## What a run does

Every `CANARY_INTERVAL_SECONDS` (15 minutes by default), one API replica:

1. **Setup.** Makes sure the canary user and the `openrag-canary` partition
   exist, removes documents an interrupted earlier run left behind, and points
   the partition back at the default embedder.
2. **Queue and index.** Writes a small text document containing a phrase
   generated for this run and submits it through the same indexing service as
   `POST /indexer/partition/{partition}/file/{file_id}`. It then waits for the
   task to finish: parsing, chunking, embedding and insertion into Milvus and
   the catalog.
3. **Query.** Asks for the phrase through the chat retrieval pipeline, using
   the partition's retriever and reranker, and checks that a chunk of this
   run's document comes back. Answer generation is not part of the check.
4. **Cleanup.** Deletes the document through the same path as
   `DELETE /indexer/partition/{partition}/file/{file_id}`, then checks that
   neither the catalog row nor any vector survived. Cleanup runs whether the
   earlier stages passed or not.

A run fails at the first stage that fails. Setup errors, a task that no
worker picks up, a failed or stuck indexing task, a retrieval that misses the
document and a delete that leaves data behind are each reported under their
own stage.

Each run uses a new file ID (`canary-<unix time>-<random>`) and a new phrase,
so a hit can only come from that run. The partition's embedder is reset to the
`default` alias before each run. This keeps the canary on the embedder that new
documents receive today: a change of default embedder is tested from the next
run onwards.

## What it does not touch

- **Other people's data.** The canary works only in the `openrag-canary`
  partition. That name is reserved: users cannot create a partition with it,
  by API call or by uploading into it. The canary deletes only its own
  documents. If a partition with that name already exists and belongs to
  someone else, which can happen on a deployment older than the reservation,
  the canary refuses to use it and fails its setup stage.
- **Anyone's quota.** Documents are uploaded as a dedicated system user,
  *OpenRag canary* (`canary@openrag.invalid`). The user has no API token, so
  nobody can sign in as it, is not an admin, and has an unlimited quota. It
  appears in the user list, and its file count rises and falls by one during
  each run.
- **Request metrics.** The canary calls the services directly, not the HTTP
  API, so its traffic never reaches `openrag_http_*`. Its indexing tasks do
  appear in the job queue and job history, under the `openrag-canary`
  partition.

## Enabling it

Set `CANARY_ENABLED=true`: in `.env` for Docker Compose, or through
`env.config.CANARY_ENABLED` in the Helm chart, where it is enabled by default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CANARY_ENABLED` | `false` (`true` in the Helm chart) | Run the canary. |
| `CANARY_INTERVAL_SECONDS` | `900` | Seconds between the starts of two runs; at least 60. |
| `CANARY_INITIAL_DELAY_SECONDS` | `60` | Wait after API startup before the first run. |
| `CANARY_INDEX_TIMEOUT_SECONDS` | `600` | Time allowed for the indexing task to finish, including time spent in the queue behind real work. |
| `CANARY_REQUEST_TIMEOUT_SECONDS` | `60` | Time allowed for each other call: submission, retrieval and deletion. |

With several API replicas, exactly one runs the canary. That replica holds a
Postgres advisory lock on a connection of its own, and exports
`openrag_canary_leader 1`. When it stops, Postgres releases the lock and
another replica takes it over at its next interval. If a replica's host
disappears without closing the connection, Postgres notices within about a
minute and a half.

## Metrics and alert

The outcome is exported on `/metrics`; see
[Prometheus metrics](/openrag/documentation/prometheus_metrics/#synthetic-canary)
for the series. The alert on them, `OpenRagCanaryFailing`, fires when two runs
in a row fail, or when no run has finished for two intervals while the canary
is enabled. Its runbook is
[OpenRagCanaryFailing](/openrag/documentation/runbooks/openrag-canary-failing/).

The rule is part of the Helm chart (`infra/charts/openrag-stack/rules/`) and
reaches Prometheus by one of two routes:

- **Kubernetes:** set `openrag.metrics.prometheusRule.enabled: true`. Use
  `labels` to match the operator's `ruleSelector`, and `ruleLabels` to add the
  labels your Alertmanager routes on.
- **Docker Compose:** the monitoring overlay mounts the same files into its
  Prometheus. That stack has no Alertmanager, so firing alerts appear only on
  Prometheus's `/alerts` page.

A canary without an alert is worse than none: its dashboard keeps showing the
last success after the canary has stopped running. Enable the rule together
with the canary.

## Limitations

- With `ENABLE_RAY_SERVE=true`, the API's metrics are not scraped (see
  [Prometheus metrics](/openrag/documentation/prometheus_metrics/#limitations)).
  The canary still runs there, but nothing alerts on it.
- The canary tests the default indexation and retrieval presets on the
  default embedder. A partition configured with a different endpoint is not
  covered; the model endpoints' readiness reports those.
- The document is plain text, so the PDF, Office, image and audio parsers are
  not exercised.
