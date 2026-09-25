# OpenRagCircuitBreakerOpen

**Severity:** critical · **Fires after:** 5 min by default

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `for.OpenRagCircuitBreakerOpen` (default 5m).
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule openrag-alerts -o yaml
> ```

```
max by (name) (openrag_circuit_breaker_state) >= 1
```

## What it means

OpenRag has **stopped calling** one of its inference endpoints after repeated failures.
Calls to it now return `outcome="circuit_open"` immediately without reaching the endpoint,
so chat and any indexing stage that depends on it fail fast rather than hanging.

The breaker is working as designed — it is protecting the system from an endpoint that is
already broken. The thing to fix is the endpoint, not the breaker.

## Read the label correctly

`name` is the breaker **kind**, declared in `services/inference`: `llm`, `embedder`,
`vlm`, `reranker`. Four code-defined values.

**It is not a registry entry name.** Looking up "llm" in the model-endpoint registry will
find nothing. To get from the kind to the endpoint actually serving it, check which
registry entry is the default for that kind, and which partition presets name a different
one — `embedder` and `chat_llm` on the partition, `reranker` and `llm` on the retrieval
preset, `vlm`, `stt` and `contextualization_llm` on the indexation preset.

## Gauge values

`0` closed · `1` open · `2` half-open · `-1` unknown. `1` and `2` both fire this: half-open means the breaker is still tripped and probing with one trial call, and a hard-down endpoint cycles between the two.

## First checks

```promql
openrag_circuit_breaker_state
sum by (provider, outcome) (rate(openrag_inference_requests_total[10m]))
```

The `outcome` split on the second query tells you what tripped it: `timeout` points at
capacity or the network, `error` at the endpoint itself.

## Likely causes

1. **The endpoint is down or unreachable.** Deployments using external inference have no
   bundled fallback.
2. **A credential expired or rotated** on the registry entry.
3. **The model was unloaded or renamed** at the provider.
4. **Sustained overload** — enough timeouts in a row to trip it.

## Recovery

The breaker half-opens on its own and closes once calls succeed, so no restart is needed
once the endpoint is healthy. Watch `openrag_circuit_breaker_state` go `1` → `2` → `0`.

## See also

[OpenRagInferenceProviderDown](OpenRagInferenceProviderDown.md) — the same outage usually
trips that first, since the breaker opens *because* calls were failing. The two are
separate alerts so that routing can inhibit one while the other is firing; expect both
during a real endpoint outage.
