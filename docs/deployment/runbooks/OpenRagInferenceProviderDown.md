# OpenRagInferenceProviderDown

**Severity:** critical · **Fires after:** 5 min by default

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `thresholds.inferenceErrorRatio` (default 0.5), `thresholds.inferenceVolumeFloor` (default 5) and `for.OpenRagInferenceProviderDown` (default 5m).
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule openrag-alerts -o yaml
> ```

```
sum by (provider) (rate(openrag_inference_requests_total{outcome=~"error|timeout"}[10m]))
/ sum by (provider) (rate(openrag_inference_requests_total{outcome=~"success|error|timeout"}[10m])) > 0.5
and sum by (provider) (increase(openrag_inference_requests_total{outcome=~"success|error|timeout"}[10m])) >= 5
```

## What it means

More than half of the calls to one registry endpoint are returning errors or timing
out — half being the default threshold — over at least 5 calls in 10 minutes, so a single
failed call on a quiet instance does not page.
Chat answers and any indexing stage depending on it will fail.

`circuit_open` is deliberately **not** counted in the ratio: it is a consequence of the
breaker, and counting it in both would keep this firing forever once the breaker tripped.
Nor are `cancelled` (the caller gave up) and `rejected` (a 4xx refusing that one
request): the ratio and the 5-call floor count only `success`, `error` and `timeout`, the
outcomes that say something about the provider.

## Read the label correctly

`provider` is the **registry entry name** — admin-created, and the string to look up in
the model-endpoint registry when this fires.

## First checks

```promql
sum by (provider, outcome) (rate(openrag_inference_requests_total[10m]))
```

The `outcome` split is the diagnosis: `timeout` points at capacity or the network, `error`
at the endpoint itself.

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

## See also

[OpenRagCircuitBreakerOpen](OpenRagCircuitBreakerOpen.md) — a sustained failure usually
trips the breaker too, at which point calls stop reaching the endpoint and this ratio
changes shape. Expect both alerts during a real outage.
