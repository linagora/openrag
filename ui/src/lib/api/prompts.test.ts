import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import {
  listAllPrompts,
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

// A single capped request hid prompts past the cap and reported a partial count
// as the total, leaving them unmanageable in the library and unselectable in
// every picker.
describe("listAllPrompts", () => {
  const page = (n: number, from: number) =>
    JSON.stringify(Array.from({ length: n }, (_, i) => ({ id: `p${from + i}`, name: `p${from + i}` })));

  it("follows pagination until a short page", async () => {
    fetchMock
      .mockResolvedValueOnce(fakeResponse({ body: page(200, 0) }))
      .mockResolvedValueOnce(fakeResponse({ body: page(200, 200) }))
      .mockResolvedValueOnce(fakeResponse({ body: page(37, 400) }));

    const all = await listAllPrompts();

    expect(all).toHaveLength(437);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toContain("offset=200");
    expect(fetchMock.mock.calls[2][0]).toContain("offset=400");
  });

  it("stops after one request when the first page is short", async () => {
    fetchMock.mockResolvedValueOnce(fakeResponse({ body: page(7, 0) }));
    expect(await listAllPrompts()).toHaveLength(7);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns an empty library without looping", async () => {
    fetchMock.mockResolvedValueOnce(fakeResponse({ body: "[]" }));
    expect(await listAllPrompts()).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
