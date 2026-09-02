---
title: Context compression
description: Shrink retrieved chunks and chat history before they reach the LLM.
---

# Context compression

Retrieved chunks are fitted to a context-token budget before the prompt is
assembled. Anything past the budget is dropped, so a relevant source can be
lost for lack of room. Compression shrinks each source instead, letting more
of the retrieval set reach the model.

Compression is off by default. On prose the gain is modest and it costs
latency on the query path, so it is opt-in per partition.

## What the model sees, and what the user sees

Only the text sent to the LLM is compressed. The sources returned to the
client keep their original content, so a citation always resolves to the real
document.

## Configuration

Two levels. The deployment-wide switch in `conf/config.yaml` decides whether
compression is available at all and which backend runs it:

```yaml
compression:
  enabled: false
  backend: noop        # noop | headroom
  target_ratio: null   # fraction of each source to keep; null = backend decides
  min_chars: 1000      # texts shorter than this are passed through
  timeout_s: 5.0
  extra: {}            # backend kwargs, e.g. {model: gpt-4o}
```

Per-partition settings live on the retrieval preset and are editable from
**Admin → Presets → Retrieval → Context compression**:

| Field | Meaning |
| --- | --- |
| `compression_enabled` | Compress this partition's retrieved chunks |
| `compression_target_ratio` | Overrides the deployment default |
| `compress_history` | Also compress older chat turns |
| `compress_history_keep_recent` | Turns left untouched at the end of the history |

Compression needs a single owning partition. Multi-partition requests and the
`openrag-all` sentinel skip it, because there is no one preset to obey.

## Backends

`noop` is the default and returns every text unchanged.

`headroom` uses [Headroom](https://github.com/headroomlabs-ai/headroom), which
routes content to a per-type compressor: statistical folding for JSON and
logs, a small ModernBERT model for prose. It runs on CPU and needs no GPU.

> **Not installable alongside the Infinity reranker client today.**
> `headroom-ai` depends on `litellm`, which requires `httpx>=0.28`, while
> `infinity-client` pins `httpx<0.28`. The package is therefore not declared
> in `pyproject.toml`. A deployment that wants it must either not use
> `infinity-client`, or override the `httpx` pin deliberately. Until then the
> backend degrades to passthrough, which is safe but does nothing.

## Adding a backend

Subclass `Compressor`, register it, and import the module from
`di/compressors.register_compressors`:

```python
from core.compression import Compressor, compressor_registry

@compressor_registry.register("mine")
class MyCompressor(Compressor):
    name = "mine"

    async def _compress(self, texts, *, options):
        return [shorten(t) for t in texts]
```

Return one string per input, in the same order. The base class handles the
rest: it enforces the count, reverts any text that came back longer, bounds
the call with `timeout_s`, and returns the originals if the backend raises. A
compression fault never fails a query.
