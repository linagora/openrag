# Issue 542: Indexing Stage Progress

## Context

Admins need to know where a slow or failed indexing job is spending time: parsing,
captioning, chunking, contextualizing, embedding, or storing vectors.

The UI should not guess this from the broad task state. A job that is still marked
`SERIALIZING` may already be parsing, captioning, embedding, or waiting on a
shared worker pool. Showing fake progress would make production debugging worse.

## Current Gap

The current system keeps live task state in the `TaskStateManager` Ray actor and
persists broad job status, timestamps, and failure information in PostgreSQL.
Terminal job records are retained for 30 days. The indexing pipeline measures
per-stage timings, but it does not persist a per-task stage timeline or expose
one through the Jobs APIs. Extending the actor alone would not meet the history
requirement: its live state and short recovery records do not have the same
retention as PostgreSQL job history.

## Proposed Shape

Add real stage events to the task state model before changing the UI. Keep the
current stage snapshot, highest emitted lifecycle source sequence, and a
`history_incomplete` marker in the `TaskStateManager` actor; PostgreSQL remains
the durable event-history source. The actor is authoritative for live stage
transitions, while PostgreSQL keeps durable history. The actor snapshot is not
a substitute for persisted history.

- task id
- event id, unique within the task and stable across a retry of the same transition write
- attempt id, shared by every transition from one stage attempt; an attempt
  starts when the pipeline reaches that stage, including a conditional decision
  to skip it
- attempt number, starting at 1 and increasing when that stage is retried or its
  conditional decision is reevaluated
- sequence number, increasing monotonically for every stage event on the task
  and assigned transactionally by the durable stage store when the event is
  first accepted
- source sequence for lifecycle transitions, increasing when the task state
  manager emits one and reused by retries; gaps reveal transitions not durably
  stored. Sampled progress snapshots do not consume this sequence
- stage identity and display name from the pipeline definition, whether the
  stage runs or is skipped; do not hard-code a global pipeline list
- status: pending, running, completed, failed, skipped, cancelled
- `stage_order`, a stable semantic ordinal from the pipeline definition, so
  the same stage keeps its rank when optional stages or document types differ
- started_at and finished_at
- emitted_at, assigned by the task state manager for live snapshots
- recorded_at, assigned by the durable stage store when it accepts the event
- duration_ms when finished
- processed and total when a stage can report it
- a sanitized public error code/summary for an unsuccessful stage; never expose
  raw exceptions or tracebacks in the task-status response
- optional counters such as pages parsed, chunks created, images captioned, and vectors inserted

Stage names and ordering come from the pipeline definition, not a hard-coded UI
list. Each pipeline definition supplies stable semantic order for its stages;
optional and document-dependent stages keep their ordering slots across
configurations and rolling deployments. Preserve existing order assignments
when adding stages. Record a conditional stage as `skipped` when the runtime
decision is known and it does not run. This keeps later stages comparable when,
for example, image captioning is enabled for one document but not another.
Represent that decision with a terminal `skipped` event and a stage attempt id
and number allocated when the pipeline reaches the stage; do not create a
placeholder attempt before the pipeline reaches it.

Stage events should be immutable and ordered by sequence number, not by client
timestamps. Each status transition is a separate event: for example, `running`
and `completed` have different event ids but share one attempt id. The durable
stage store assigns the sequence number and `recorded_at` when it first accepts
an event, and the writer preserves emission order per task. After a stage starts, each meaningful progress update is another
immutable `running` event with a new event id and the same attempt id; do not
update an accepted event in place. Emit progress snapshots at bounded,
meaningful increments rather than for every item processed. Retrying the same
write reuses its event id; an identical duplicate is an accepted no-op. If the
same `(task_id, event_id)` arrives with a different payload, preserve the
original event and report an explicit conflict asynchronously to the task state
manager. Mark the actor's history `incomplete` and monotonically set the same
flag on the durable job record, including after the job has settled. If the
actor has been evicted, the durable flag remains authoritative; if storage is
temporarily unavailable, retry the marker update under the bounded writer
policy. Record the conflict for diagnosis, but do not fail or delay indexing
because of it; do not treat the write as accepted.

Running a stage again is different from retrying an event write. A real stage
retry creates a new attempt id and increments the attempt number, preserving the
previous attempt as history. A worker restart follows the same rule unless it is
only resending a transition that was already accepted. Before a restarted worker
begins a new attempt, recovery must close any previous `pending` or `running`
attempt in the authoritative task state with a terminal `failed` event and a
sanitized worker-interruption code; persist that event asynchronously. Preserve
the stage snapshot, source sequence, and `history_incomplete` marker in actor
recovery snapshots, including cancelled task snapshots. A retry must never
leave the previous attempt looking active beside the new one.

Valid transition sequences within one attempt are intentionally narrow:

- pending to running, skipped, failed, cancelled
- running to running (progress snapshot), completed, failed, skipped, cancelled
- a terminal status never changes; another execution uses a new attempt

Create a stage attempt only when that stage is next to run; do not pre-create
pending attempts for every later stage. Keep at most one non-terminal stage
attempt per task so the API can identify the current stage unambiguously.

This keeps the timeline readable when a stage is retried: the first attempt can
end as failed, and the later attempt can complete without producing impossible
states such as a single event moving from failed back to running.

Cancelling a task must close every active `pending` or `running` stage attempt.
Apply a terminal `cancelled` event to the authoritative task state before the
broad task is reported as cancelled, then enqueue it for durable history. Include
its finish time and a sanitized reason code. If no pending or running attempt
exists, no cancellation event is needed. If persistence fails, the live API
still reports the cancelled stage and `history_incomplete`; it must not show a
cancelled job with a stage still running.

The existing task-status URL returned by upload and replacement endpoints stays
unchanged. `GET /indexer/task/{task_id}` continues to return the current broad
task fields (`task_id`, `task_state`, `details`, and `error_url` when relevant)
and is augmented with a `stages` field containing the ordered transition events,
grouped by attempt id, plus a compact `stage_progress` snapshot. Stage error
fields in this response must be safe for the task owner to read. Keep raw
tracebacks behind the existing error endpoint's admin-only response; ordinary
owners continue to receive its generic message.
For active tasks and terminal tasks still retained by the actor, overlay the
actor's latest stage snapshot on durable job data; use PostgreSQL for the event
timeline and terminal-task history. The task-status and task-list APIs must
follow this same source rule. If the actor has evicted a terminal task and its
durable event history does not reach the job's lifecycle watermark, report an
unknown stage with `history_incomplete`, never a stale active stage. The active
list must not wait for the durable writer to catch up before showing the actor
snapshot. Old clients that only poll the broad task state can keep working.

The Jobs page uses `GET /queue/tasks` for its list. Add a compact `stage_progress`
summary to each row in that existing response so the Active list does not need a
separate status request for every task. It includes the current stage, its
pipeline-defined `stage_order`, display position and stage count, and last update
within that task's resolved pipeline. A broad `queued` task remains queued and
has no current stage. For active tasks, select the single non-terminal attempt.
If there is no active attempt during a stage handoff, report `unknown` or
`starting` with no current-stage counters; do not present the previous completed
stage as current. For terminal tasks, select the latest terminal attempt so
failed jobs still identify where they stopped.
Within the selected durable attempt, take the latest transition by sequence
number and use its `recorded_at` as the last update. For a live actor snapshot,
use its server-assigned `emitted_at`. Use the latest non-null `processed` and
`total` counters in that attempt; when none exist, leave them absent. Task
status and list responses must use the same selection rule.

Include `history_truncated` in both responses: it is a top-level boolean in task
status and a per-row boolean in `/queue/tasks`. Set it true when older events
were pruned, and false for tasks with no stage data. Also expose
`history_incomplete` when an emitted lifecycle event is missing from durable
history, either detected on write failure or by a sequence gap/watermark; normal
coalescing of intermediate progress snapshots does not set this flag. Keep it
distinct from retention pruning. Tasks created before stage tracking or with no
emitted stage events default to complete history. Sort active jobs
by `stage_order` descending, using the pipeline-defined semantic order rather
than a normalized fraction of pipeline length. For jobs in the same semantic
stage, compare `processed / total` when both have valid counters; use the
server-assigned last update only when within-stage progress is unavailable or
equal. For different stages, use `stage_order` first and recency only as a
tie-breaker. Tasks without a reported stage come after stage-reported tasks and
before queued jobs. Put queued jobs last, and use task id ascending as the final
deterministic tie-breaker. This is a stage-based ordering, not a time-to-
completion estimate. Tasks without a reported stage remain visibly `unknown` or
`starting`; do not infer a stage for them.

Persist append-only stage events in PostgreSQL through the job persistence layer;
the Ray actor may provide live updates, but must not be the only history store.
Use a job-linked event table (or an equivalent durable representation) with
unique `(task_id, event_id)`, `(task_id, sequence)`, and
`(task_id, source_sequence)` keys. Link the events to
their job with cascading deletion so the existing 30-day terminal-job cleanup
also removes stage history. Cap each task at 200 events; when pruning older
transitions, remove old event payloads without changing retained events and keep
their event id, payload digest, and original sequence in a compact deduplication
record through the job's 30-day retention window; retain the source sequence
there as well so pruning does not look like a missing event. A matching delayed retry is an
accepted no-op; the same id with a different digest is a conflict, and a pruned
event is never inserted with a new sequence. Bound and coalesce progress
snapshots so the deduplication records stay manageable. Mark the response's
`history_truncated` field true after pruning. Older tasks without stage data
return an empty `stages` array, no progress summary, and
`history_truncated: false` rather than a guessed timeline.

Stage-event persistence must never change the indexing result or make
indexation wait on a slow database. Apply lifecycle transitions immediately to
the authoritative `TaskStateManager` state, then send them through a reserved
per-task queue to the bounded background writer. Coalesce or drop intermediate
progress snapshots first under backpressure. If a lifecycle event cannot be
queued or its bounded storage retries are exhausted, keep the live state
transition, set `history_incomplete`, log and count the loss, and persist the
marker with the job when storage is available. On task settlement,
persist the highest emitted lifecycle source sequence with the terminal job
update. If durable lifecycle events have a source-sequence gap or do not reach
that watermark, expose and persist `history_incomplete`. On writer restart,
compare the actor's source sequence with persisted events. On worker restart,
reconcile orphaned
non-terminal jobs, close persisted active attempts, and mark their history
incomplete because queued writes may have been lost. This lets the signal
survive writer and worker restarts without blocking stage execution.
Return that marker distinctly from `history_truncated`, which only means normal
retention pruning. A writer conflict result must use the same non-blocking actor
notification and durable marker path, without waiting for either write in the
indexing task; updating the durable flag is allowed after the job reaches a
terminal state. Status reads must include the durable marker after actor
eviction or restart. The writer retries transient storage failures within a
bounded policy. A database error or event conflict must not fail a core stage,
turn a successful enrichment into a degraded stage, or replace the original
indexing error. Read stage summaries in bulk for the task IDs returned by
`/queue/tasks`; the page polls every five seconds and the durable list may
contain 500 rows plus live actor tasks, so avoid one database query per row.

## Follow-Up Work

1. Add durable stage-event storage and bulk read APIs to the PostgreSQL job
   persistence layer; keep the current stage snapshot and incomplete marker in
   `TaskStateManager` for live reads.
2. Emit stage transitions with the resolved pipeline position from the indexing
   pipeline, including retries, cancellation, and worker interruption.
3. Preserve the existing 30-day job-history policy and bounded per-task stage
   history, including a visible indication when old events were pruned.
4. Keep broad task states and existing task-status polling compatible.
5. Add the `stages` field to task status and the `stage_progress` summary to
   `/queue/tasks`; merge live actor snapshots for active tasks and use durable
   PostgreSQL history for terminal tasks. Keep raw tracebacks on the existing
   admin-only error path.
6. Add the Jobs-page stage badge, detail timeline, and active-job ordering only
   after the API returns persisted stage data.
7. Test owner/admin error visibility, actor restart recovery (including a
   cancelled task and delayed writer), bounded cleanup, old tasks with no stage
   data, and sorting with optional pipeline stages and
   within-stage counters. Verify the same stage keeps its order across config
   and document types, including captioning on an image and off on a text file;
   verify the UI can display an unknown new stage from API metadata.
8. Delay and fail stage-event writes while tasks are active; verify status and
   list still show the actor's latest stage, and verify cancellation or retry
   never leaves the previous attempt shown as active. Verify lifecycle-sequence
   gaps, retry exhaustion, task settlement, and worker restart expose
   `history_incomplete`; normal progress coalescing must not set that flag.
   Inject a conflicting duplicate after task settlement; verify the original
   event remains unchanged, indexing still succeeds, and `history_incomplete`
   remains visible after actor eviction or restart.
   Inject stage-store failures during core and enrichment work; verify
   they do not change indexed content or task outcomes, and measure that
   indexing throughput and latency stay within an agreed tolerance of the same
   pipeline without stage tracking. Verify the list reads stage summaries in a
   bounded number of queries for 500 durable rows plus any live actor tasks.

## Non-Goal

Do not infer stage progress from logs, task state names, or client-side timers.
Those signals are not reliable enough for production debugging.
