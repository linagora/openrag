import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import {
  listPrompts,
  getPrompt,
  createPrompt,
  updatePrompt,
  setPromptDefault,
  deletePrompt,
} from "./prompts";

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

function lastCall() {
  const [url, init] = fetchMock.mock.calls.at(-1)!;
  return { url: url as string, init: (init ?? {}) as RequestInit };
}

describe("listPrompts", () => {
  it("hits /prompts/ and returns the bare array", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: '[{"id":"1","name":"a","used_by":2}]' }));
    const result = await listPrompts();
    expect(lastCall().url).toBe("/prompts/");
    expect(result).toEqual([{ id: "1", name: "a", used_by: 2 }]);
  });

  it("serializes type/offset/limit into the query string", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: "[]" }));
    await listPrompts({ prompt_type: "sys_prompt", offset: 10, limit: 50 });
    expect(lastCall().url).toBe("/prompts/?prompt_type=sys_prompt&offset=10&limit=50");
  });
});

describe("CRUD verbs and paths", () => {
  it("getPrompt → GET /prompts/{id}", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: '{"id":"abc"}' }));
    await getPrompt("abc");
    const { url, init } = lastCall();
    expect(url).toBe("/prompts/abc");
    expect(init.method ?? "GET").toBe("GET");
  });

  it("createPrompt → POST /prompts/ with a JSON body", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 201, body: '{"id":"new"}' }));
    await createPrompt({ prompt_type: "hyde", name: "x", content: "c" });
    const { url, init } = lastCall();
    expect(url).toBe("/prompts/");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ prompt_type: "hyde", name: "x", content: "c" });
  });

  it("updatePrompt → PATCH /prompts/{id}", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: '{"id":"abc"}' }));
    await updatePrompt("abc", { content: "new" });
    const { url, init } = lastCall();
    expect(url).toBe("/prompts/abc");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ content: "new" });
  });

  it("setPromptDefault → PUT /prompts/{id}/default", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: '{"id":"abc","is_default":true}' }));
    await setPromptDefault("abc");
    const { url, init } = lastCall();
    expect(url).toBe("/prompts/abc/default");
    expect(init.method).toBe("PUT");
  });

  it("deletePrompt → DELETE /prompts/{id} (204)", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 204, body: "" }));
    await deletePrompt("abc");
    const { url, init } = lastCall();
    expect(url).toBe("/prompts/abc");
    expect(init.method).toBe("DELETE");
  });

  it("url-encodes the id", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: "{}" }));
    await getPrompt("a b/c");
    expect(lastCall().url).toBe("/prompts/a%20b%2Fc");
  });
});
