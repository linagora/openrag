# Issue 542: Indexing Stage Progress

## Context

Admins need to know where a slow or failed indexing job is spending time: parsing,
captioning, chunking, contextualizing, embedding, or storing vectors.

The UI should not guess this from the broad task state. A job that is still marked
`SERIALIZING` may already be parsing, captioning, embedding, or waiting on a
shared worker pool. Showing fake progress would make production debugging worse.

## Current Gap

Today the task state manager keeps only:

- broad task state
- task details
- traceback on failure
- worker/queue totals

The indexing pipeline already measures per-stage timings, but those timings are
written to logs only. They are not persisted per task and are not available from
the Jobs APIs after refresh, completion, failure, or worker restart.

## Proposed Shape

Add real stage events to the task state model before changing the UI:

- task id
- event id, unique within the task and stable across a retry of the write
- attempt number, starting at 1 and increasing when a stage is retried
- sequence number, assigned by the state manager and increasing monotonically
  for every stage event on the task
- stage name
- status: pending, running, completed, failed, skipped
- started_at and finished_at
- duration_ms when finished
- processed and total when a stage can report it
- last_error for the failed stage
- optional counters such as pages parsed, chunks created, images captioned, and vectors inserted

Stage events should be append-only and ordered by sequence number, not by client
timestamps. A worker restart or retry must create a new attempt instead of
rewriting the previous failed attempt. Duplicate writes for the same event id
with the same payload should be treated as already applied; a duplicate event id
with conflicting payload should be rejected or logged as a state-write bug, not
shown as another timeline row.

Valid status movement is intentionally narrow:

- pending to running, skipped, failed
- running to completed, failed, skipped
- failed to a new attempt for the same stage

This keeps the timeline readable when a stage is retried: the first attempt can
end as failed, and the later attempt can complete without producing impossible
states such as a single event moving from failed back to running.

The existing task-status URL returned by upload and replacement endpoints stays
unchanged. `GET /indexer/task/{task_id}` continues to return the current broad
task fields (`task_id`, `task_state`, `details`, and `error_url` when relevant)
and is augmented with a `stages` field containing the ordered stage history. Old
clients that only poll the broad task state can keep working, while newer clients
can render the timeline from the same task-status response.

Stage history must also be bounded. Keep stage events at least as long as the
task remains visible in task history, but cap retention with a configurable TTL
and a per-task event limit. The proposed defaults are 30 days and 200 events per
task. When the event limit is exceeded, keep the latest events and enough summary
data to explain the final stage state. Cleanup should run from the same
task-history cleanup path, so old stage data does not grow forever after
completed or failed jobs. Older tasks that predate stage tracking should return
an empty `stages` array rather than a guessed timeline.

## Follow-Up Work

1. Extend `TaskStateManager` with stage-event storage and read APIs.
2. Emit start/finish/fail events from the indexing pipeline stages.
3. Preserve stage history after completion and failure.
4. Keep broad task states unchanged for compatibility.
5. Add the `stages` field to the existing task-status response.
6. Add UI timeline only after the API returns real persisted stage data.

## Non-Goal

Do not infer stage progress from logs, task state names, or client-side timers.
Those signals are not reliable enough for production debugging.
