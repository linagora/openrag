import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getTaskStageProgress } from "./jobs";

function fakeResponse(body = "{}"): Response {
  return {
    status: 200,
    ok: true,
    headers: { get: (name: string) => (name.toLowerCase() === "content-type" ? "application/json" : null) },
    text: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getTaskStageProgress", () => {
  it("serializes the history cursor and page size", async () => {
    fetchMock.mockResolvedValue(fakeResponse());

    await getTaskStageProgress("task/1", { attempt: 3, sequence: 7 }, 25);

    const requestUrl = String(fetchMock.mock.calls[0][0]);
    const url = new URL(requestUrl, window.location.origin);
    expect(url.pathname).toBe("/indexer/task/task%2F1/stages");
    expect(Array.from(url.searchParams.entries())).toEqual([
      ["limit", "25"],
      ["before_attempt", "3"],
      ["before_sequence", "7"],
    ]);
  });

  it("omits cursor parameters for the current summary page", async () => {
    fetchMock.mockResolvedValue(fakeResponse());

    await getTaskStageProgress("task-1", null, 1);

    const url = new URL(String(fetchMock.mock.calls[0][0]), window.location.origin);
    expect(Array.from(url.searchParams.entries())).toEqual([["limit", "1"]]);
  });
});
