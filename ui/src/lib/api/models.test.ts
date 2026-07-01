import { describe, it, expect } from "vitest";
import { pickDefaultEndpoint, resolveEmbedderName } from "./models";
import type { ModelEndpointResponse } from "./models";

function ep(name: string, is_default = false): ModelEndpointResponse {
  return {
    name,
    model_type: "embedder",
    endpoint: "http://endpoint",
    model_name: null,
    batch_size: 32,
    timeout: 30,
    extra: {},
    is_default,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
  };
}

describe("pickDefaultEndpoint", () => {
  it("returns undefined for empty / nullish input", () => {
    expect(pickDefaultEndpoint(undefined)).toBeUndefined();
    expect(pickDefaultEndpoint(null)).toBeUndefined();
    expect(pickDefaultEndpoint([])).toBeUndefined();
  });

  it("returns the is_default endpoint when one is flagged", () => {
    expect(pickDefaultEndpoint([ep("a"), ep("b", true)])?.name).toBe("b");
  });

  it("returns the sole endpoint when exactly one is registered", () => {
    expect(pickDefaultEndpoint([ep("only")])?.name).toBe("only");
  });

  it("returns undefined when several exist and none is default (user must choose)", () => {
    expect(pickDefaultEndpoint([ep("a"), ep("b")])).toBeUndefined();
  });
});

describe("resolveEmbedderName", () => {
  const endpoints = [ep("jina", true), ep("other")];

  it("returns the value verbatim when it isn't the 'default' sentinel", () => {
    expect(resolveEmbedderName("my-embedder", endpoints)).toBe("my-embedder");
  });

  it("shows an em dash for null / empty", () => {
    expect(resolveEmbedderName(null, endpoints)).toBe("—");
    expect(resolveEmbedderName(undefined, endpoints)).toBe("—");
    expect(resolveEmbedderName("", endpoints)).toBe("—");
  });

  it("resolves the 'default' sentinel to the real default endpoint name", () => {
    expect(resolveEmbedderName("default", endpoints)).toBe("jina");
  });

  it("falls back to 'default' when the default can't be resolved", () => {
    expect(resolveEmbedderName("default", [ep("a"), ep("b")])).toBe("default");
    expect(resolveEmbedderName("default", [])).toBe("default");
    expect(resolveEmbedderName("default", null)).toBe("default");
  });

  it("keeps 'default' when the default endpoint is itself named 'default'", () => {
    expect(resolveEmbedderName("default", [ep("default", true)])).toBe("default");
  });
});
