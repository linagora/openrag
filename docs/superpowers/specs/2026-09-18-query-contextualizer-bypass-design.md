# Query Contextualizer Bypass for Retrieval Evaluation

## Context

Retrieval benchmarks need to isolate the effect of query rewriting. Comparing raw search with the chat pipeline is not sufficient because it can also change hybrid retrieval, reranking, filtering, and output depth. The existing original-query shadow trace measures ranking quality, but OpenRAG still runs the contextualizer and the normal retrieval path. It therefore cannot represent the latency, cost, or behavior of a request where query contextualization is genuinely disabled.

## Decision

OpenRAG will expose a request-scoped diagnostic option named `bypass_query_contextualization`. It will be disabled by default and accepted only for authenticated administrators when retrieval tracing and required retrieval are also enabled.

When enabled, OpenRAG will skip query-contextualizer prompt resolution and inference. The latest user message will become the single retrieval query and will continue through the same partition scope, filters, hybrid retrieval, reranker, expansion settings, and final result limits as the contextualized path.

The option will not modify presets or server configuration. It will not affect later requests, ordinary chat traffic, or the defaults used by existing clients.

## API contract

The option belongs to chat-completion request metadata. It is valid only when `include_retrieval_trace` and `require_retrieval` are both true. It is mutually exclusive with `compare_original_query`, because one request must either run the contextualized path with its optional shadow comparison or run the bypassed path.

Invalid combinations will return a client error before inference or retrieval begins. The completions endpoint will continue rejecting retrieval diagnostic options because this behavior is defined only for chat messages.

The existing administrative authorization and diagnostic rate limit will apply. No new production-facing capability is enabled for partition viewers.

## Trace semantics

The version-one trace will add an optional `bypassed` contextualization field. Existing traces remain valid and consumers that do not know the field can continue reading the rest of the contract.

For a bypassed request:

- the trace records the exact original user message;
- `bypassed` is true and `fallback_used` is false;
- contextualizer model, prompt identity, intent, and generated subqueries are absent because they were not evaluated;
- the original-query stage is complete;
- the contextualized-query stage is explicitly not run;
- retrieval, reranking, and final stages are recorded normally.

The trace must not describe the bypass as a contextualizer fallback. A fallback means contextualization was attempted and failed; a bypass means it was intentionally not attempted.

## Evaluator behavior

The benchmark will expose three reranked query modes:

- `contextualized` sends one normal traced chat request and scores its returned retrieval results;
- `original` sends one bypassed traced chat request and scores retrieval from the unchanged benchmark question;
- `compare` sends both requests for each question and calculates paired metrics.

The compare mode will keep the two requests adjacent within each query worker while existing benchmark concurrency continues across questions. It will verify that both traces report the same retrieval-configuration fingerprint. A mismatched pair will be marked incompatible rather than used for a metric delta.

Each variant retains its own result IDs, ranks, scores, latency, trace, and error. Aggregate output reports original and contextualized metrics separately, then reports paired deltas only for questions where both variants completed under matching configurations. Missing telemetry or one-sided failures are reported as unavailable and never treated as retrieval misses.

The comparison classifies measured outcomes as improved, regressed, unchanged, both missed, or unavailable. Multi-gold datasets retain normal recall and nDCG semantics. Metric cutoffs remain limited by the effective result depth of each variant.

## Operational impact

Contextualized and original modes each execute one chat request per question. Compare mode executes two and therefore increases retrieval, reranking, and minimal answer-generation work. It remains explicit and is not the default.

The bypass removes the contextualizer inference cost only from the original request. This makes per-variant latency meaningful, although the benchmark will still present retrieval-stage and total request latency separately.

## Failure handling

A bypassed request that cannot retrieve is handled like any other retrieval request. Trace serialization failures remain non-fatal to retrieval, but a comparison without the trace needed to verify configuration compatibility cannot contribute to paired deltas.

If the contextualized variant succeeds and the original variant fails, its contextualized metrics remain valid. The reverse also applies. Comparison coverage is always shown alongside paired metrics to prevent partial results from appearing complete.

## Security and compatibility

The bypass is administrative, opt-in, rate-limited, and content-free in telemetry. It does not expose prompt content, document bodies, credentials, internal endpoints, or additional corpus metadata. Existing clients and historical traces remain compatible because the request option defaults to false and the new trace field is optional.

## Verification

Deterministic tests will confirm that bypassed requests never call the contextualizer, use the exact latest user message for retrieval, still execute the configured reranker, produce truthful trace states, reject unsafe option combinations, enforce administrative authorization, and leave normal chat behavior unchanged. Evaluator tests will cover all three modes, paired configuration checks, partial failures, multi-gold metrics, and persisted rank transitions without requiring live inference services.
