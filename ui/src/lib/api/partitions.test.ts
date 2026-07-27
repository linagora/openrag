import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createPartition, listPartitionMemberCandidates } from "./partitions";

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
          candidates: [{ user_id: 2, display_name: "Sam" }],
        }),
      }),
    );

    await expect(listPartitionMemberCandidates("legal docs")).resolves.toEqual({
      candidates: [{ user_id: 2, display_name: "Sam" }],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/partition/legal%20docs/users/candidates"),
      expect.any(Object),
    );
  });
});
