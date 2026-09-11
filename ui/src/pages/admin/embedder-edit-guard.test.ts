import { describe, it, expect } from "vitest";

import { diffEndpointUpdate, hasMaterialChange, suggestCopyName } from "./embedder-edit-guard";
import type { ModelEndpointResponse, UpdateModelEndpointRequest } from "@/lib/api/models";

const embedder = {
  name: "qwen",
  model_type: "embedder",
  endpoint: "https://a.example/v1",
  model_name: "Qwen3-Embedding-0.6B",
  batch_size: 32,
  timeout: 30,
} as unknown as ModelEndpointResponse;

const update = (over: UpdateModelEndpointRequest): UpdateModelEndpointRequest => ({
  endpoint: embedder.endpoint,
  model_name: embedder.model_name ?? undefined,
  batch_size: embedder.batch_size,
  timeout: embedder.timeout,
  ...over,
});

describe("diffEndpointUpdate", () => {
  it("reports nothing when the form was submitted unchanged", () => {
    expect(diffEndpointUpdate(embedder, update({}))).toEqual([]);
  });

  it("flags a model change as material", () => {
    const changes = diffEndpointUpdate(embedder, update({ model_name: "bge-m3" }));

    expect(changes).toHaveLength(1);
    expect(changes[0]).toMatchObject({
      field: "model_name",
      from: "Qwen3-Embedding-0.6B",
      to: "bge-m3",
      material: true,
    });
    expect(hasMaterialChange(changes)).toBe(true);
  });

  it("flags a URL change as material", () => {
    // The name stays, so nothing else in the schema records that the model
    // behind it moved — which is exactly why this needs confirming.
    const changes = diffEndpointUpdate(embedder, update({ endpoint: "https://b.example/v1" }));

    expect(hasMaterialChange(changes)).toBe(true);
  });

  it("does not treat a rename as material", () => {
    // #770 cascades a rename to every stored reference; the model is untouched.
    const changes = diffEndpointUpdate(embedder, update({ name: "qwen-prod" }));

    expect(changes).toHaveLength(1);
    expect(changes[0].material).toBe(false);
    expect(hasMaterialChange(changes)).toBe(false);
  });

  it("does not treat throughput knobs as material", () => {
    // They change how vectors are fetched, never what they are. Demanding an
    // acknowledgement here is what trains people to tick it unread.
    const changes = diffEndpointUpdate(embedder, update({ batch_size: 64, timeout: 60 }));

    expect(changes.map((c) => c.field)).toEqual(["batch_size", "timeout"]);
    expect(hasMaterialChange(changes)).toBe(false);
  });

  it("never marks a non-embedder change material", () => {
    // Repointing an LLM changes future answers, never a stored vector.
    const llm = { ...embedder, model_type: "llm" } as ModelEndpointResponse;

    const changes = diffEndpointUpdate(llm, update({ model_name: "mistral" }));

    expect(changes).toHaveLength(1);
    expect(hasMaterialChange(changes)).toBe(false);
  });

  it("reads a cleared model as 'not set' rather than reporting no change", () => {
    const changes = diffEndpointUpdate(embedder, update({ model_name: undefined }));

    expect(changes).toHaveLength(1);
    expect(changes[0]).toMatchObject({ to: "not set", material: true });
  });

  it("orders material changes ahead of cosmetic ones", () => {
    const changes = diffEndpointUpdate(
      embedder,
      update({ name: "qwen-prod", model_name: "bge-m3", timeout: 60 }),
    );

    expect(changes.map((c) => c.field)).toEqual(["model_name", "name", "timeout"]);
  });
});

describe("suggestCopyName", () => {
  it("suggests a free name so the safe path is one click", () => {
    expect(suggestCopyName("qwen", ["qwen"])).toBe("qwen-v2");
  });

  it("skips names already taken", () => {
    expect(suggestCopyName("qwen", ["qwen", "qwen-v2", "qwen-v3"])).toBe("qwen-v4");
  });

  it("does not stack suffixes when copying a copy", () => {
    expect(suggestCopyName("qwen-v2", ["qwen", "qwen-v2"])).toBe("qwen-v3");
  });
});
