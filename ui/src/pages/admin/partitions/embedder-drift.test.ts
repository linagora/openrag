import { describe, it, expect } from "vitest";

import { computeEmbedderDrift, driftSignature } from "./embedder-drift";
import type { ModelEndpointResponse } from "@/lib/api/models";

// `default` is a virtual alias: no endpoint row is named "default", the row
// flagged is_default answers to it. A partition may store the alias, and a file
// records the reference as the partition had it — the alias included — next to
// the model it ran. Everything below turns on comparing models, not labels.
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

  it("flags files sitting in another endpoint's field, same model or not", () => {
    // Two endpoints, one model, a field each (#762 F): searches read the
    // partition's field only, so the other endpoint's files are missing from
    // results even though every model name on screen matches.
    const twoEndpoints = [
      { name: "qwen", model_name: "Qwen3-Embedding-0.6B", vector_field: "vector_qwen" },
      { name: "qwen-b", model_name: "Qwen3-Embedding-0.6B", vector_field: "vector_qwen_b" },
    ] as ModelEndpointResponse[];

    const drift = computeEmbedderDrift(
      "qwen",
      [
        {
          embedder: "qwen",
          model_name: "Qwen3-Embedding-0.6B",
          dimension: 1024,
          vector_field: "vector_qwen",
          file_count: 5,
        },
        {
          embedder: "qwen-b",
          model_name: "Qwen3-Embedding-0.6B",
          dimension: 1024,
          vector_field: "vector_qwen_b",
          file_count: 2,
        },
      ],
      twoEndpoints,
    );

    expect(drift.hasDrift).toBe(true);
    expect(drift.driftedFiles).toBe(2);
  });

  it("leaves files with no recorded field alone", () => {
    // Indexed before the field was recorded: unknown, not known-bad.
    const drift = computeEmbedderDrift(
      "qwen",
      [{ embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 4 }],
      endpoints,
    );

    expect(drift.hasDrift).toBe(false);
  });

  const twoWidths = [
    { embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 7 },
    { embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 768, file_count: 2 },
  ];

  it("splits one model served at two widths, and flags the width the collection lacks", () => {
    // Two vector spaces under one name. The collection is 1024-d, so the 768-d
    // files are not in the index queries search; the 1024-d ones are fine.
    const drift = computeEmbedderDrift("qwen", twoWidths, endpoints, 1024);

    expect(drift.groups).toHaveLength(2);
    expect(drift.groups.map((g) => g.dimension)).toEqual([1024, 768]);
    // Same name on screen, so the list needs something else to key them by.
    expect(new Set(drift.groups.map((g) => g.id)).size).toBe(2);
    expect(drift.drifted.map((g) => g.dimension)).toEqual([768]);
    expect(drift.driftedFiles).toBe(2);
  });

  it("does not call a width drifted when the live dimension is unknown", () => {
    // Nothing to compare against: unknown, not known-bad.
    const drift = computeEmbedderDrift("qwen", twoWidths, endpoints, null);

    expect(drift.groups).toHaveLength(2);
    expect(drift.hasDrift).toBe(false);
  });

  it("does not call a partition on the alias drifted before the endpoint list is known", () => {
    // Non-admins never get the list, and admins see the partition before it
    // loads. "default" then names nothing, so "qwen" is not evidence of drift.
    const drift = computeEmbedderDrift("default", [{ embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 4 }], undefined);
    expect(drift.hasDrift).toBe(false);
  });

  it("does not call a file recorded under the alias drifted before the endpoint list is known", () => {
    const drift = computeEmbedderDrift("qwen", [{ embedder: "default", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 4 }], undefined);
    expect(drift.hasDrift).toBe(false);
  });

  it("does not call a renamed endpoint drifted before the endpoint list is known", () => {
    // A non-admin cannot resolve the current model. The partition follows the
    // rename; the file keeps the name it was indexed under. Different labels,
    // same model — nothing on screen may call that drift.
    const drift = computeEmbedderDrift(
      "qwen-renamed",
      [{ embedder: "qwen", model_name: "Qwen3-Embedding-0.6B", dimension: 1024, file_count: 4 }],
      undefined,
      1024,
    );
    expect(drift.hasDrift).toBe(false);
  });

  it("does not call a file with no recorded model drifted by its label alone", () => {
    // The endpoint it names is gone and no model was recorded: unknown.
    const drift = computeEmbedderDrift("qwen", [{ embedder: "deleted-endpoint", model_name: null, dimension: 1024, file_count: 3 }], endpoints, 1024);
    expect(drift.hasDrift).toBe(false);
  });

  it("still flags a width the collection lacks when the alias cannot be resolved", () => {
    // The width comes from the collection, not the endpoint list.
    const drift = computeEmbedderDrift("default", twoWidths, undefined, 1024);
    expect(drift.drifted.map((g) => g.dimension)).toEqual([768]);
  });

  it("gives a drift at another width a signature of its own", () => {
    // Dismissing 768-d drift must not also dismiss a later 512-d one with the
    // same model and file count.
    const at = (dimension: number) =>
      computeEmbedderDrift("qwen", [{ embedder: "bge-m3", model_name: "bge-m3", dimension, file_count: 2 }], endpoints);
    expect(driftSignature(at(768))).not.toBe(driftSignature(at(512)));
    expect(driftSignature(computeEmbedderDrift("qwen", [], endpoints))).toBe("");
  });

  it("gives a drift in another field a signature of its own", () => {
    // Same model, width and file count, but sitting in another endpoint's
    // field: dismissing one must not dismiss the other.
    const fields = [
      { name: "qwen", model_name: "Qwen3-Embedding-0.6B", vector_field: "vector_qwen" },
      { name: "qwen-b", model_name: "Qwen3-Embedding-0.6B", vector_field: "vector_qwen_b" },
      { name: "qwen-c", model_name: "Qwen3-Embedding-0.6B", vector_field: "vector_qwen_c" },
    ] as ModelEndpointResponse[];
    const inField = (embedder: string, vector_field: string) =>
      computeEmbedderDrift(
        "qwen",
        [{ embedder, model_name: "Qwen3-Embedding-0.6B", dimension: 1024, vector_field, file_count: 2 }],
        fields,
      );
    expect(inField("qwen-b", "vector_qwen_b").hasDrift).toBe(true);
    expect(driftSignature(inField("qwen-b", "vector_qwen_b"))).not.toBe(
      driftSignature(inField("qwen-c", "vector_qwen_c")),
    );
  });

  it("has nothing to say about an empty partition", () => {
    expect(computeEmbedderDrift("qwen", [], endpoints).hasDrift).toBe(false);
    expect(computeEmbedderDrift("qwen", undefined, endpoints).groups).toEqual([]);
  });
});
