# Per-file indexing stage timings

## Context

OpenRAG already measures the time spent in individual indexing stages, but completed task responses do not include those measurements. The benchmark can therefore confirm that files completed while its Pipeline stage breakdown remains empty. Operators need the stage timings after a run to see where indexing time went.

## Goal

Make per-file stage durations available from the completed task status API so benchmark reports can show parse, captioning, chunking, contextualizing, topic tagging, embedding, and storing time.

## Data contract

Task details may include a `stage_timings` object keyed by the existing pipeline stage names. Each value is a finite, non-negative duration in seconds. Stages that were skipped are omitted. Older tasks without timings remain valid.

Durations are wall-clock measurements for that file and may include time waiting on shared worker capacity. They are not CPU-only measurements, and totals summed over concurrent files may exceed the run’s elapsed time.

## Lifecycle and compatibility

The worker’s existing per-stage measurements must travel with the task’s terminal result. Task state should expose them while retained in memory, and the durable job record should preserve them for later status requests. A failure to write optional history must not turn successful indexing into a failed task.

The API addition is optional and backward-compatible. Existing task records and clients that ignore `stage_timings` continue to work. The dashboard and benchmark already treat missing timing data as unavailable; a new benchmark run against the updated service is required to collect the values.

## Acceptance

- Successful tasks expose measured durations for the stages they executed.
- Failed tasks expose the durations measured before and during the failing stage when available.
- Completed task timings remain available through the durable task-status path.
- Missing timings on older records do not break task-status responses.
- The benchmark can consume the returned values and show stage totals for a new run.

## Verification

Cover worker capture, terminal task details, durable persistence and retrieval, and compatibility with records that have no timing data. Confirm a benchmark run reports timings for tasks whose OpenRAG response includes them.
