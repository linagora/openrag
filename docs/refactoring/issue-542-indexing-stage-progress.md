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
- stage name
- status: pending, running, completed, failed, skipped
- started_at and finished_at
- duration_ms when finished
- processed and total when a stage can report it
- last_error for the failed stage
- optional counters such as pages parsed, chunks created, images captioned, and vectors inserted

The task detail API can then expose a stable `stages` array. The Jobs list can show
the current running stage, and the Job detail page can show the full timeline.

## Follow-Up Work

1. Extend `TaskStateManager` with stage-event storage and read APIs.
2. Emit start/finish/fail events from the indexing pipeline stages.
3. Preserve stage history after completion and failure.
4. Keep broad task states unchanged for compatibility.
5. Add API fields for stage history.
6. Add UI timeline only after the API returns real persisted stage data.

## Non-Goal

Do not infer stage progress from logs, task state names, or client-side timers.
Those signals are not reliable enough for production debugging.
