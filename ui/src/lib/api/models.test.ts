import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import {
  displayModelEndpointExtra,
  mergeModelEndpointApiKeyExtra,
  mergeModelEndpointLlmContext,
  mergeModelEndpointSttLanguage,
  pickDefaultEndpoint,
  prepareModelEndpointExtraForSubmit,
  revealModelEndpointApiKey,
  resolveEmbedderName,
  splitModelEndpointApiKeyExtra,
  splitModelEndpointLlmContext,
  splitModelEndpointSttLanguage,
  validateModelEndpoint,
} from "./models";
import type { ModelEndpointResponse } from "./models";

function fakeResponse({
  status = 200,
  body = "{}",
}: { status?: number; body?: string } = {}): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (key: string) => (key.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => JSON.parse(body),
    text: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

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

describe("validateModelEndpoint", () => {
  it("can request draft validation with a stored server-side API key", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: JSON.stringify({ reachable: true }) }));

    await validateModelEndpoint({
      endpoint: "http://candidate:8000/v1",
      model_type: "stt",
      model_name: "mistral-small",
      timeout: 900,
      stored_api_key_model_type: "llm",
      stored_api_key_name: "private-llm",
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      endpoint: "http://candidate:8000/v1",
      model_type: "stt",
      model_name: "mistral-small",
      timeout: 900,
      stored_api_key_model_type: "llm",
      stored_api_key_name: "private-llm",
    });
  });
});

describe("revealModelEndpointApiKey", () => {
  it("requests the admin-only reveal endpoint for the selected model endpoint", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: JSON.stringify({ api_key: "secret-token" }) }));

    const result = await revealModelEndpointApiKey("llm", "private-llm");

    expect(result).toEqual({ api_key: "secret-token" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/model-endpoints/llm/private-llm/reveal-api-key",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("model endpoint secret placeholders", () => {
  it("shows backend redacted secret sentinels as password-style bullets", () => {
    expect(
      displayModelEndpointExtra({
        auth: { token: "<redacted>" },
        backend_secret: "<redacted>",
        headers: [{ api_key: "<redacted>" }],
        note: "<redacted>",
      }),
    ).toEqual({
      auth: { token: "••••••••" },
      backend_secret: "••••••••",
      headers: [{ api_key: "sk-********" }],
      note: "<redacted>",
    });
  });

  it("keeps backend prefix-masked secret values visible without revealing the full secret", () => {
    expect(
      displayModelEndpointExtra({
        auth: { token: "nes********" },
        headers: [{ api_key: "hf-********" }],
        note: "abc********",
      }),
    ).toEqual({
      auth: { token: "nes********" },
      headers: [{ api_key: "hf-********" }],
      note: "abc********",
    });
  });

  it("converts unchanged bullet placeholders back to the backend redacted sentinel", () => {
    expect(
      prepareModelEndpointExtraForSubmit({
        auth: { token: "••••••••" },
        headers: [{ api_key: "hf-********" }],
        note: "••••••••",
      }),
    ).toEqual({
      auth: { token: "<redacted>" },
      headers: [{ api_key: "<redacted>" }],
      note: "••••••••",
    });
  });

  it("still accepts the previous API key placeholder when an edit form is already open", () => {
    expect(prepareModelEndpointExtraForSubmit({ api_key: "********" })).toEqual({
      api_key: "<redacted>",
    });
  });

  it("splits the API key out of endpoint extra for the dedicated form field", () => {
    expect(
      splitModelEndpointApiKeyExtra({
        api_key: "sk-real-secret",
        implementation: "vllm",
        temperature: 0.2,
      }),
    ).toEqual({
      apiKey: "sk-********",
      extra: {
        implementation: "vllm",
        temperature: 0.2,
      },
    });
  });

  it("merges the dedicated API key field back into the endpoint extra payload", () => {
    expect(
      mergeModelEndpointApiKeyExtra(
        {
          implementation: "vllm",
        },
        "sk-new-secret",
      ),
    ).toEqual({
      implementation: "vllm",
      api_key: "sk-new-secret",
    });
  });

  it("preserves a stored API key when the dedicated field is unchanged", () => {
    expect(
      mergeModelEndpointApiKeyExtra(
        {
          implementation: "vllm",
        },
        "sk-********",
      ),
    ).toEqual({
      implementation: "vllm",
      api_key: "<redacted>",
    });
  });

  it("submits an explicit empty API key when clearing an existing stored key", () => {
    expect(
      mergeModelEndpointApiKeyExtra(
        {
          implementation: "vllm",
        },
        "",
        { clearApiKey: true },
      ),
    ).toEqual({
      implementation: "vllm",
      api_key: "",
    });
  });

  it("omits an empty API key when creating an endpoint without a stored key", () => {
    expect(
      mergeModelEndpointApiKeyExtra(
        {
          implementation: "vllm",
        },
        "",
      ),
    ).toEqual({
      implementation: "vllm",
    });
  });
});

describe("LLM context token-budget extra fields", () => {
  it("splits the two budgets out of extra into form-field strings", () => {
    expect(
      splitModelEndpointLlmContext({
        implementation: "vllm",
        max_llm_context_size: 32768,
        max_output_tokens: 2048,
      }),
    ).toEqual({
      llmContext: { maxContextSize: "32768", maxOutputTokens: "2048" },
      extra: { implementation: "vllm" },
    });
  });

  it("yields blank fields when the budgets are absent", () => {
    expect(splitModelEndpointLlmContext({ implementation: "vllm" })).toEqual({
      llmContext: { maxContextSize: "", maxOutputTokens: "" },
      extra: { implementation: "vllm" },
    });
  });

  it("merges non-blank budget fields back into extra as numbers", () => {
    expect(
      mergeModelEndpointLlmContext(
        { implementation: "vllm" },
        { maxContextSize: "32768", maxOutputTokens: "2048" },
      ),
    ).toEqual({
      implementation: "vllm",
      max_llm_context_size: 32768,
      max_output_tokens: 2048,
    });
  });

  it("clears a budget key when its field is left blank", () => {
    expect(
      mergeModelEndpointLlmContext(
        { implementation: "vllm", max_llm_context_size: 8192, max_output_tokens: 1024 },
        { maxContextSize: "16384", maxOutputTokens: "" },
      ),
    ).toEqual({
      implementation: "vllm",
      max_llm_context_size: 16384,
    });
  });

  it("round-trips split → merge without touching unrelated extra keys", () => {
    const stored = { implementation: "vllm", api_key: "sk-x", max_output_tokens: 512 };
    const { llmContext, extra } = splitModelEndpointLlmContext(stored);
    expect(mergeModelEndpointLlmContext(extra, llmContext)).toEqual({
      implementation: "vllm",
      api_key: "sk-x",
      max_output_tokens: 512,
    });
  });
});

describe("STT language-hint extra field", () => {
  it("splits and merges a language hint without touching provider options", () => {
    const { languageHint, extra } = splitModelEndpointSttLanguage({
      language: "fr",
      diarization: true,
    });

    expect(languageHint).toBe("fr");
    expect(extra).toEqual({ diarization: true });
    expect(mergeModelEndpointSttLanguage(extra, "en")).toEqual({
      diarization: true,
      language: "en",
    });
  });

  it("removes the language key when the dedicated field is blank", () => {
    expect(mergeModelEndpointSttLanguage({ language: "fr", diarization: true }, "  ")).toEqual({
      diarization: true,
    });
  });
});
