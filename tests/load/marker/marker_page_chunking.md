# Marker Page Chunking Benchmark

## Objective

Measure the impact of `MARKER_CHUNK_SIZE` on PDF parsing speed and GPU memory usage. Page chunking splits large PDFs into fixed-size page ranges and dispatches them across all available Marker workers in parallel, rather than sending the entire file to a single worker.

## Setup

### Worker configuration

| Variable | Value |
|----------|-------|
| `MARKER_MAX_PROCESSES` | 5 |
| `MARKER_MAX_TASKS_PER_CHILD` | 100 |

### Disabled features

The following features were disabled to isolate the measurement to pure PDF parsing time, avoiding bias from downstream processing:

```bash
CONTEXTUAL_RETRIEVAL=false
IMAGE_CAPTIONING=false
VDB_ENABLE_INSERTION=false
```

### Dataset

| Metric | Min | Max | Mean | Std |
|--------|-----|-----|------|-----|
| Pages per PDF | 11 | 40 | 21.0 | 8.3 |
| Size per PDF (MB) | 0.03 | 25.81 | 1.89 | 3.84 |

## Results

Tested with `MARKER_CHUNK_SIZE` values of 10, 20, and 30

| Chunk size | Parsing duration | Max GPU spike (GB) | Spike duration |
|------------|-----------------|---------------------|----------------|
| 30 | 16m 27s | 2.2 - 4.0 | 5s to ~2 min (file-dependent) |
| 20 | 16m 44s | 2.2 - 3 | 5s to ~1 min |
| 10 | 17m 29s | 1.9 - 2.5 | 5s - 30s |

## Analysis

### Speed

These results are at **equal number of workers** (`MARKER_MAX_PROCESSES=5`). All chunk sizes perform similarly (~16-17 min), and with smaller chunks the workload per worker actually *increases* since each worker handles more tasks (more chunks to process).

The main advantage of chunking is therefore not raw speed at fixed worker count, but the ability to **scale the number of workers without risking OOM**. By keeping per-worker memory spikes low and spike duration low aswell, chunking allows safely increasing `MARKER_MAX_PROCESSES`, which is where the real speed gains come from.

The GPU memory constraint can be estimated as:

```text
available_gpu_mem >= max_spike * num_workers + marker_model_gpu_size + other_gpu_processes
```

With a chunk size of 10 (max spike ~2.2 GB) you can fit more workers in the same GPU budget than with unchunked processing (where spikes can reach 4+ GB per worker).

### GPU memory

Smaller chunk sizes produce **lower and shorter memory spikes**:

- **Chunk size 10**: Safest option. Peak stays around 1.9-2.5 GB with spikes lasting at most 30 seconds. Memory drops quickly since 10 pages are processed fast.
- **Chunk size 20-30**: Spikes can reach 3-4 GB and persist for up to 2 minutes, increasing the risk of OOM when multiple workers hit peak usage simultaneously.

### Spike behavior

- Spikes occur primarily during Marker's **"Recognizing text"** phase.
- For chunk sizes 20 and 30, spike duration can extend to ~2 minutes with peaks between 2.3 and 4 GB. This raises OOM risk when several processes spike concurrently.
- Files with complex or non-searchable text are the worst case: Marker spends significantly more time in the recognition phase (layout recognition, text recognition, OCR error detection, bbox detection), keeping memory elevated for longer.

## Recommendation

A chunk size of **10** offers the best trade-off: parsing speed is comparable to larger chunks, while GPU memory stays controlled with short-lived spikes. This reduces OOM risk in production, especially under concurrent load.