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
  timeout_s: 15.0
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
logs, a small ModernBERT model for prose. Retrieved chunks are prose, so the
model path is the one that matters here. It runs on CPU and needs no GPU.

### Installing it

`headroom-ai` is declared as an optional extra:

```bash
uv sync --extra compression
```

It also declares `litellm`, which requires `httpx>=0.28` and so collides with
`infinity-client`'s `httpx<0.28` pin. Headroom uses litellm only for its model
registry and non-core providers, all lazily imported and ImportError-guarded;
the compression path never touches it, and upstream itself ships without it on
Python 3.14. A uv override in `pyproject.toml` therefore drops it. Everything
else the prose compressor needs (transformers, onnxruntime, huggingface-hub)
is already in the dependency set.

The model is fetched from HuggingFace on first use. Construction kicks off a
background load so the download never blocks startup, which means the first
few requests in a fresh process pass through uncompressed. Set
`extra: {warmup: false}` to skip it, and pre-seed the HuggingFace cache in the
image if you need compression active from the first request.

### What it costs

Measured on a 10-core CPU, 400-word chunks:

| target_ratio | Tokens saved | Latency |
| --- | --- | --- |
| null (model decides) | ~13% | ~490 ms per chunk |
| 0.7 | ~19% | ~490 ms per chunk |
| 0.5 | ~43% | ~490 ms per chunk |
| 0.3 | ~57% | ~490 ms per chunk |

Latency is linear in the number of chunks and independent of the ratio, so a
partition retrieving `top_n: 10` pays roughly five seconds per query. Size
`timeout_s` above that or the compressor will spend the time and then pass the
originals through anyway. This is why compression is off by default and why it
is worth enabling mainly where the retrieval set would otherwise be truncated,
or on partitions where answer latency is not interactive.

Compression is lossy. At aggressive ratios it strips function words, so
sources read as clipped notes rather than prose. Check answer quality on your
own corpus before turning it on widely.

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
