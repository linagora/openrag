import { describe, it, expect } from "vitest";

import { computeEmbedderDrift } from "./embedder-drift";
import type { ModelEndpointResponse } from "@/lib/api/models";

// `default` is a virtual alias: no endpoint row is named "default", the row
// flagged is_default answers to it. A partition stores the alias; a file
// records whatever it resolved to at index time. Everything below turns on
// resolving both sides before comparing.
const endpoints = [
  { name: "qwen", model_type: "embedder", model_name: "Qwen3-Embedding-0.6B", is_default: true },
  { name: "bge-m3", model_type: "embedder", model_name: "bge-m3", is_default: false },
] as unknown as ModelEndpointResponse[];

describe("computeEmbedderDrift", () => {
  it("reports no drift when every file matches the configured embedder", () => {
    const drift = computeEmbedderDrift("qwen", [{ embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 9 }], endpoints);
    expect(drift.hasDrift).toBe(false);
    expect(drift.driftedFiles).toBe(0);
  });

  it("does not flag a partition on the 'default' alias as drifted from itself", () => {
    // Configured "default", files recorded "qwen" — the same endpoint. Comparing
    // the raw strings would make every aliased partition look broken.
    const drift = computeEmbedderDrift("default", [{ embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 4 }], endpoints);
    expect(drift.hasDrift).toBe(false);
    // Named by the model, not the endpoint: the panel compares models, so
    // reporting the verdict against a label would name a thing it never checked.
    expect(drift.current).toBe("Qwen3-Embedding-0.6B");
  });

  it("flags files recorded against a different embedder, and counts them", () => {
    const drift = computeEmbedderDrift(
      "qwen",
      [
        { embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 9 },
        { embedder: "bge-m3", model_name: "bge-m3", dimension: 1024, file_count: 2 },
      ],
      endpoints,
    );
    expect(drift.hasDrift).toBe(true);
    expect(drift.driftedFiles).toBe(2);
    expect(drift.drifted.map((g) => g.key)).toEqual(["bge-m3"]);
  });

  it("treats files indexed before provenance existed as unknown, not drifted", () => {
    // A null embedder predates the record. Counting it as drift would fire the
    // dialog on every partition indexed before this shipped.
    const drift = computeEmbedderDrift("qwen", [{ embedder: null, model_name: null, dimension: null, file_count: 12 }], endpoints);
    expect(drift.hasDrift).toBe(false);
    expect(drift.groups.map((g) => g.key)).toEqual(["unrecorded"]);
  });

  it("does not flag files indexed before the endpoint was renamed", () => {
    // Observed on a real deployment: one endpoint, renamed at some point, so
    // older files recorded the old label while newer ones recorded the new one.
    // The same model produced all of them — `model_name` is identical — and a
    // label comparison would have reported the older files as drifted and
    // popped a warning on a healthy partition. #770 cascades a rename to
    // `partitions.embedder`, but a file's provenance is a historical record
    // and is deliberately never rewritten.
    const drift = computeEmbedderDrift(
      "qwen",
      [
        { embedder: "Qwen3-Embedding-0.6B", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 10 },
        { embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 1 },
      ],
      endpoints,
    );
    expect(drift.hasDrift).toBe(false);
    // And they merge into one entry: one model, one row, whatever it was called
    // when each file was indexed.
    expect(drift.groups).toHaveLength(1);
    expect(drift.groups[0]).toMatchObject({ key: "Qwen3-Embedding-0.6B", file_count: 11 });
  });

  it("still flags a real model change recorded under an unknown endpoint name", () => {
    // The endpoint is gone, but the model it ran was recorded at index time —
    // which is the whole reason E stores `model_name` and not just the label.
    const drift = computeEmbedderDrift(
      "qwen",
      [{ embedder: "deleted-endpoint", model_name: "e5-large", dimension: 768, file_count: 3 }],
      endpoints,
    );
    expect(drift.hasDrift).toBe(true);
    expect(drift.driftedFiles).toBe(3);
  });

  it("has nothing to say about an empty partition", () => {
    expect(computeEmbedderDrift("qwen", [], endpoints).hasDrift).toBe(false);
    expect(computeEmbedderDrift("qwen", undefined, endpoints).groups).toEqual([]);
  });
});
