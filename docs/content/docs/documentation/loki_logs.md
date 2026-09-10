---
title: Loki logs
description: Ship OpenRAG logs to an external Loki and read them in Grafana.
---

# Loki logs

OpenRAG writes its logs to the process stderr and nowhere else. Docker and
the kubelet capture that stream; a collector (Grafana Alloy, …)
parses it and pushes it to Loki, where Grafana reads it. With
`LOG_FORMAT=json` every line is one flat JSON object the collector can
index without guessing.

"stderr" is the conventional diagnostic channel, not a severity: `INFO`
lines go there too. The severity is the `level` field; never filter on the
`stream` label.

## Log line format

```json
{"ts":"2026-09-07T14:03:12.481000+00:00","level":"INFO","logger":"api.routers.user.chat","function":"chat_completions","line":212,"msg":"Retrieved 8 documents","request_id":"req_7f3c…","partition":"docs"}
```

| Field | Meaning |
| --- | --- |
| `ts` | ISO-8601 with timezone. |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `logger`, `function`, `line` | Call site. For uvicorn/Ray lines routed through the app logger, `logger` is their original name (`uvicorn.access`). |
| `msg` | The message. |
| `exception` | Formatted traceback, only when one is attached. |
| `request_id` | Present on every line emitted inside `RequestIdMiddleware` — all route and auth logs, streaming bodies included, and the `Unhandled exception` line of a 500 (bound explicitly there); echoed to the client as `X-Request-ID`. Two kinds of line carry none: those from the outer Instrumentation and RequestTimeout middlewares, and uvicorn's own `uvicorn.access` line, which is emitted when the response starts, after the middleware has returned. An inbound `X-Request-ID` is reused only when it matches `^[A-Za-z0-9_.:-]{1,128}$`; otherwise a fresh `req_…` is minted. |
| `task_id` | Present on indexing worker lines. |
| any other key | A field bound with `logger.bind()`; a name colliding with a reserved key is prefixed `extra_`. |

Ray relays its workers' output onto the API process with a
`(Indexer pid=4242) ` prefix; the pipelines below strip it before parsing
and keep it as `ray_actor`. Two Ray defaults must be off for the relay to
carry valid JSON, and the compose overlay and the Helm chart set both:
`RAY_DEDUP_LOGS=0` (Ray's deduplication merges same-shape lines from several
workers and rewrites the survivor as `{…} [repeated 2x across cluster]`, which
is no longer JSON) and `RAY_COLOR_PREFIX=0` (the prefix is otherwise
ANSI-colorized even on a pipe). Both pipelines still strip escape sequences
first, before the prefix regex, as a belt-and-braces. Some lines are not JSON
(the startup banner, Ray's own startup messages); they are shipped as-is with
no `level` label.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `LOG_FORMAT` | `text` | `json` for collectors. The compose logging overlay and the Helm chart set it; a plain compose stack keeps the colorized text. |
| `LOG_LEVEL` | `INFO` | `DEBUG` records user queries; use it briefly. |
| `RAY_DEDUP_LOGS` | `1` (Ray) | Must be `0` with `LOG_FORMAT=json`: the overlay and the chart set it. See above. |
| `RAY_COLOR_PREFIX` | `1` (Ray) | Set to `0` by the overlay and the chart so the relay prefix is plain text. |
| `RAY_LOG_TO_STDERR` | unset | Set to `1` by the chart when `ray.enabled=true`: workers write to their own pod's stderr instead of the API driver's relay. See the Kubernetes section. |

JSON mode also routes every library's stdlib logs to the collector (the
four named ones — `asyncio`, `httpcore`, `httpx`, `urllib3` — capped at
WARNING), so with a collector attached `LOG_LEVEL=DEBUG` is a
troubleshooting setting, not a production one: everything else logs at the
level you set, and the ingest volume follows.

## Loki requirements

The pipelines below store `request_id`, `task_id` and `ray_actor` as
[structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/),
which needs **Loki ≥ 3.0** with `allow_structured_metadata: true` (the
default from 3.0 on) and a **v13 TSDB** schema. Against an older Loki, or
one where the option is off, the push is rejected — and Loki rejects the
*whole batch*, so those log lines are lost entirely, not merely stripped of
their metadata. Nothing surfaces in Grafana; the only trace is an error in
the collector's own log (`docker logs openrag-alloy`).

If your Loki cannot be upgraded or reconfigured, drop the
`stage.structured_metadata` block from the pipeline. The fields then stay in
the JSON body and the queries become `| json | request_id="…"` — the
`stage.json` stage already extracts them, and query-time filtering costs
more but works on any Loki version.

## VM: the compose logging overlay

```bash
cd infra/compose
docker compose -f docker-compose.yaml -f logging.docker-compose.yaml up -d
```

The overlay adds an Alloy container reading the Docker socket and sets
`LOG_FORMAT=json` on the API. The socket is mounted `:ro`, which protects
the socket *file* only — any container holding it has the full Docker API,
i.e. root on the host — so treat Alloy as a trusted component. It needs, in
`.env`:

| Variable | Required | Meaning |
| --- | --- | --- |
| `LOKI_URL` | yes | Push endpoint, e.g. `https://loki.example.com/loki/api/v1/push`. |
| `LOKI_USERNAME`, `LOKI_PASSWORD` | no | Basic auth. |
| `LOKI_TENANT_ID` | no | Sent as `X-Scope-OrgID`. |

`LOKI_URL` and the other `LOKI_*` variables are read by Compose
interpolation, so they must live in `infra/compose/.env` (or the file passed
to `--env-file`), not only in a file pointed at by `SHARED_ENV`.

The pipeline (`infra/compose/alloy/config.alloy`) keeps the `openrag` and
`openrag-cpu` containers, sets the labels `app=openrag`, `service`,
`container`, `level`, and stores `request_id`, `task_id` and `ray_actor` as
structured metadata. To ship Milvus, Postgres or nginx too, widen the
`keep` regex in `discovery.relabel`. Check it works:

```bash
docker logs openrag-alloy 2>&1 | tail
```

and in Grafana Explore, data source Loki: `{app="openrag"} | json`.

## Kubernetes: the platform collector

The chart sets `LOG_FORMAT=json` and ships no collector: it relies on a
DaemonSet (Alloy, Fluent Bit, …) that already reads every pod's
stdout. **This is a prerequisite, not an option.** Earlier releases kept a
`ReadWriteMany` logs volume mounted into the API and every Ray node so the
task-log route could read a shared `app.json`; that volume is gone, so on a
cluster without a collector the logs exist only in the container runtime
(`kubectl logs`) and are lost with the pod. Install the collector first,
then add the OpenRAG parsing stages to it for the pods labelled
`app.kubernetes.io/name=openrag`.

Alloy (`loki.process`; adjust the `stage.match` stream selector to the
labels your collector puts on the OpenRAG pods):

```alloy
stage.replace {
  expression = "([[:cntrl:]]\\[[0-9;]*m)"
  replace    = ""
}
stage.match {
  selector = "{app_kubernetes_io_name=\"openrag\"} |~ \"^\\\\([^)]*pid=\""

  stage.regex {
    expression = "^\\((?P<ray_actor>[^)]*)\\) (?P<line>.*)$"
  }
  stage.output {
    source = "line"
  }
}
stage.json {
  expressions = {
    ts         = "ts",
    level      = "level",
    request_id = "request_id",
    task_id    = "task_id",
  }
  drop_malformed = false
}
stage.labels {
  values = { level = "" }
}
stage.structured_metadata {
  values = {
    request_id = "",
    task_id    = "",
    ray_actor  = "",
  }
}
stage.timestamp {
  source            = "ts"
  format            = "RFC3339Nano"
  action_on_failure = "skip"
}
```

With `ray.enabled=true` the workers run in their own pods and the chart sets
`RAY_LOG_TO_STDERR=1` on them: each worker writes straight to its pod's
stderr, without the `(Actor pid=N)` prefix, and the DaemonSet ships those
lines with the Ray pods' labels. Apply the same pipeline to the pods
labelled `ray.io/cluster`, or the worker lines (the ones carrying `task_id`)
reach Loki unparsed — with no `level` label and no `task_id` metadata. Do not
rely on the API pod's relay in this mode: Ray only relays the output of
actors created by the *current* driver job, and the worker actors are
detached and reused across API restarts, so after the first restart the
relay goes silent for them. On this path Ray's own component logs (raylet,
GCS, dashboard agent) come out on the same stderr; ship them as-is, or drop
them with a `stage.drop` on lines that are not JSON if the volume matters.

## Reading logs in Grafana

| Question | LogQL |
| --- | --- |
| Everything from OpenRAG | `{app="openrag"} \| json` |
| Errors only | `{app="openrag", level="ERROR"} \| json` |
| One request (from the `X-Request-ID` header) | `{app="openrag"} \| request_id="req_7f3c…"` |
| One indexing task | `{app="openrag"} \| task_id="<task_id>"` |
| Worker lines only | `{app="openrag"} \| ray_actor!=""` |

`ray_actor` can be present as an empty value on non-worker lines (the
structured-metadata stage always sets the key), so `| ray_actor!=""` is the
right filter to isolate worker lines, as used above.

## Upgrading from a release with file logs

The chart no longer declares the `<release>-logs` PVC that backed
`/app/logs`. Because `persistence.annotations` carries
`helm.sh/resource-policy: keep` by default, Helm does **not** delete it on
upgrade: the PVC (and its PV) survive, still bound and still billed, simply
unmounted. Nothing in OpenRAG writes to it any more, so remove it by hand
once you have kept whatever you wanted from it:

```bash
kubectl delete pvc <release>-logs
```

## Not covered

- Logs of Milvus, Postgres, vLLM, the reranker and nginx: they log to
  their own stdout; add them to the collector if you need them.
- Ray's internal logs (`/tmp/ray/session_*/logs` inside the container).
- Durability of a failed task's traceback. `GET /indexer/task/{task_id}/error`
  reads the task state held by the `TaskStateManager` Ray actor, and `FAILED`
  is not among the states it persists across an actor restart, so the
  traceback is gone after one. Until job state is mirrored to Postgres
  (pull request #904), Loki is the only durable record of why a job failed.
- `ENABLE_RAY_SERVE=true`. Ray Serve configures the `ray.serve` logger
  inside every replica *after* the app module has installed the JSON
  interception, so the replicas' per-request lines come out as text; with
  the Chainlit UI on top, the second uvicorn server started for Chainlit
  re-applies uvicorn's own logging configuration the same way. The default
  single-uvicorn deployment (`uvicorn api.main:app`, what the container
  entrypoint runs) is not affected. The `python -m api.main` development
  path has the same inversion: with `ENABLE_RAY_SERVE` unset it calls
  `uvicorn.run(..., reload=True)` after the app module has already
  installed the JSON sink, so uvicorn's own lines revert to text there too.
- Very long lines in the relay path (compose, or the chart with
  `ray.enabled=false`). Ray's relay thread and the API's sink write to the
  same stderr without a shared lock; a relayed worker line above roughly
  8 KiB — a traceback inlined in `exception`, a DEBUG dump — can have an API
  line spliced into it before its newline, and both records then fail to
  parse. Below that size the writes are atomic. With `RAY_LOG_TO_STDERR=1`
  each worker writes its own stderr and the API's lines are not involved.
