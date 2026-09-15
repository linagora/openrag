# OpenRagInferenceProviderDown

**Severity:** critical · **Fires after:** 5 min

```
(sum by (provider) (rate(openrag_inference_requests_total{outcome=~"error|timeout"}[10m]))
 / sum by (provider) (rate(openrag_inference_requests_total[10m]))) > 0.5
or
label_replace(max by (name) (openrag_circuit_breaker_state) == 1, "provider", "$1", "name", "(.*)")
```

## What it means

An inference endpoint is failing more than half its calls, **or** its circuit breaker has
opened. Chat answers and any indexing stage that depends on it will fail.

## First: which of the two signals fired?

The alert unions two conditions, and they need different responses.

```promql
openrag_circuit_breaker_state                                  # 1 = open
sum by (provider, outcome) (rate(openrag_inference_requests_total[10m]))
```

- **Breaker open** (`openrag_circuit_breaker_state == 1`) — OpenRag has stopped calling
  the endpoint. Calls now return `outcome="circuit_open"` immediately. Fix the endpoint;
  the breaker half-opens and recovers on its own.
- **Error ratio** — OpenRag is still calling and mostly failing. Look at the `outcome`
  split: `timeout` points at capacity or the network, `error` at the endpoint itself.

`circuit_open` is deliberately **not** counted in the error ratio: it is a consequence of
the breaker, and counting it in both would keep the alert firing forever once tripped.

## A label-space caveat

The two sides carry related but not identical labels. `name` on the breaker is the breaker
**kind** — `llm`, `embedder`, `vlm`, `reranker` — while `provider` on the request counter
is the **registry entry name**. The rule projects `name` onto `provider` so the annotation
resolves either way; do not assume the two strings mean the same thing.

## Likely causes

1. **The external endpoint is down or unreachable.** Deployments that use external
   inference have no bundled fallback.
2. **A credential expired or rotated** — the registry entry's API key.
3. **The model was unloaded or renamed** at the provider.
4. **The endpoint is overloaded** — `timeout` dominating rather than `error`.

## A trap worth knowing

`/ready`'s model checks currently probe the **default** endpoint for each kind, not the
one a given partition's preset names (#945). A green `/ready` does not mean the endpoint
serving that tenant is healthy, and a partition whose preset names a deleted endpoint
silently falls back to the default. Check the registry entry named in the alert, not the
readiness output.

## Verify recovery

Error ratio falls below 50% and `openrag_circuit_breaker_state` returns to 0 (closed).
