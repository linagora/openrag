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
- event id, unique within the task and stable across a retry of the same transition write
- attempt id, shared by every transition from one execution of a stage
- attempt number, starting at 1 and increasing when the stage is run again
- sequence number, assigned by the state manager and increasing monotonically
  for every stage event on the task
- stage name
- status: pending, running, completed, failed, skipped, cancelled
- started_at and finished_at
- duration_ms when finished
- processed and total when a stage can report it
- last_error or cancellation reason for an unsuccessful stage
- optional counters such as pages parsed, chunks created, images captioned, and vectors inserted

Stage events should be immutable and ordered by sequence number, not by client
timestamps. Each status transition is a separate event: for example, `running`
and `completed` have different event ids but share one attempt id. The state
manager assigns the sequence number when it first accepts an event. Retrying the
same write reuses its event id; an identical duplicate is already applied, while
a conflicting duplicate is rejected or logged as a state-write bug.

Running a stage again is different from retrying an event write. A real stage
retry creates a new attempt id and increments the attempt number, preserving the
previous attempt as history. A worker restart follows the same rule unless it is
only resending a transition that was already accepted.

Valid transition sequences within one attempt are intentionally narrow:

- pending to running, skipped, failed, cancelled
- running to completed, failed, skipped, cancelled
- a terminal status never changes; another execution uses a new attempt

This keeps the timeline readable when a stage is retried: the first attempt can
end as failed, and the later attempt can complete without producing impossible
states such as a single event moving from failed back to running.

Cancelling a task must also close any active stage attempt. The cancellation
path appends a terminal `cancelled` event with its finish time and reason before
the broad task is reported as cancelled. If no stage has started, no synthetic
stage event is needed. This prevents a cancelled task from returning a timeline
whose last stage remains `running` indefinitely.

The existing task-status URL returned by upload and replacement endpoints stays
unchanged. `GET /indexer/task/{task_id}` continues to return the current broad
task fields (`task_id`, `task_state`, `details`, and `error_url` when relevant)
and is augmented with a `stages` field containing the ordered transition events,
grouped by attempt id. Old clients that only poll the broad task state can keep
working, while newer clients can render the timeline from the same task-status
response.

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
3. Preserve stage history after completion, failure, and cancellation.
4. Keep broad task states unchanged for compatibility.
5. Add the `stages` field to the existing task-status response.
6. Add UI timeline only after the API returns real persisted stage data.

## Non-Goal

Do not infer stage progress from logs, task state names, or client-side timers.
Those signals are not reliable enough for production debugging.
