import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  cancelEmbedderSwap,
  createPartition,
  getEmbedderSwap,
  listPartitionFiles,
  listPartitionMemberCandidates,
  startEmbedderSwap,
} from "./partitions";

// Minimal Response-like object covering what `request` reads (mirrors client.test.ts).
function fakeResponse({ status = 200, body = "" }: { status?: number; body?: string } = {}): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => (body ? JSON.parse(body) : {}),
    text: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

function methodOf(call: unknown[]): string {
  return ((call[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase();
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createPartition", () => {
  it("rolls back the created partition when applying config fails", async () => {
    // POST creates the empty partition, the config PATCH fails — the partition
    // must be deleted so no orphaned, half-configured partition is left behind.
    fetchMock.mockImplementation((_url: string, opts?: RequestInit) => {
      const method = (opts?.method ?? "GET").toUpperCase();
      if (method === "PATCH") {
        return Promise.resolve(fakeResponse({ status: 400, body: JSON.stringify({ detail: "bad embedder" }) }));
      }
      return Promise.resolve(fakeResponse({ status: method === "POST" ? 201 : 200 }));
    });

    await expect(createPartition({ name: "p", embedder: "bad" })).rejects.toThrow();

    const methods = fetchMock.mock.calls.map(methodOf);
    expect(methods).toContain("POST"); // partition was created
    expect(methods).toContain("PATCH"); // config attempted
    expect(methods).toContain("DELETE"); // ...then rolled back
  });

  it("does not delete when the config applies successfully", async () => {
    fetchMock.mockImplementation((_url: string, opts?: RequestInit) => {
      const method = (opts?.method ?? "GET").toUpperCase();
      if (method === "PATCH") {
        return Promise.resolve(fakeResponse({ status: 200, body: JSON.stringify({ name: "p" }) }));
      }
      return Promise.resolve(fakeResponse({ status: 201 }));
    });

    await createPartition({ name: "p", embedder: "good" });

    expect(fetchMock.mock.calls.map(methodOf)).not.toContain("DELETE");
  });
});

describe("listPartitionMemberCandidates", () => {
  it("requests the owner-protected candidate endpoint", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({
        body: JSON.stringify({
          candidates: [{ user_id: 2, display_name: "Sam", email: "sam@example.com" }],
          limit: 10,
          has_more: true,
          next_cursor: 30,
        }),
      }),
    );

    await expect(
      listPartitionMemberCandidates("legal docs", {
        search: "Sam Lee",
        cursor: 20,
        limit: 10,
      }),
    ).resolves.toEqual({
      candidates: [{ user_id: 2, display_name: "Sam", email: "sam@example.com" }],
      limit: 10,
      has_more: true,
      next_cursor: 30,
    });
    const requestedUrl = String(fetchMock.mock.calls[0][0]);
    expect(requestedUrl).toContain("/partition/legal%20docs/users/candidates?");
    expect(Array.from(new URLSearchParams(requestedUrl.split("?")[1]).entries())).toEqual(
      expect.arrayContaining([
        ["search", "Sam Lee"],
        ["cursor", "20"],
        ["limit", "10"],
      ]),
    );
  });
});

describe("listPartitionFiles", () => {
  it("passes the bounded degraded-stage filter to the catalog endpoint", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: JSON.stringify({ files: [] }) }));

    await listPartitionFiles("legal docs", { limit: 20, degradedStage: "caption" });

    const requestedUrl = String(fetchMock.mock.calls[0][0]);
    expect(requestedUrl).toContain("/partition/legal%20docs?");
    expect(Array.from(new URLSearchParams(requestedUrl.split("?")[1]).entries())).toEqual([
      ["limit", "20"],
      ["degraded_stage", "caption"],
    ]);
  });
});

describe("embedder swap", () => {
  const swap = {
    partition: "docs",
    source_embedder: "e5",
    target_embedder: "bge-m3",
    status: "running",
    files_total: 4,
    files_done: 1,
    error: null,
    started_at: "2026-09-14T00:00:00Z",
    updated_at: "2026-09-14T00:00:00Z",
    finished_at: null,
  };

  it("reads a partition that never swapped as no swap, not an error", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: "null" }));

    await expect(getEmbedderSwap("docs")).resolves.toBeNull();
    expect(fetchMock.mock.calls[0][0]).toBe("/partition/docs/embedder-swap");
  });

  it("surfaces other failures", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 500, body: JSON.stringify({ detail: "boom" }) }));

    await expect(getEmbedderSwap("docs")).rejects.toThrow("boom");
  });

  it("starts a swap with the target embedder in the body", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 202, body: JSON.stringify(swap) }));

    await expect(startEmbedderSwap("my docs", "bge-m3")).resolves.toEqual(swap);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/partition/my%20docs/embedder-swap");
    expect(methodOf(fetchMock.mock.calls[0])).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ embedder: "bge-m3" });
  });

  it("cancels with DELETE", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: JSON.stringify({ ...swap, status: "cancelled" }) }));

    await cancelEmbedderSwap("docs");

    expect(methodOf(fetchMock.mock.calls[0])).toBe("DELETE");
  });
});
