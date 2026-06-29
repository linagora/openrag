import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { request, ApiError, TOKEN_KEY } from "./client";

// Build a minimal Response-like object covering exactly what `request` reads.
function fakeResponse({
  status = 200,
  body = "",
  contentType = "application/json",
}: { status?: number; body?: string; contentType?: string } = {}): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => (body ? JSON.parse(body) : {}),
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

describe("api client", () => {
  it("defaults to a same-origin (relative) URL — never an absolute localhost fallback", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: JSON.stringify({ ok: true }) }));
    await request("/users/info");
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe("/users/info");
    expect(url.startsWith("http")).toBe(false); // would leak the bearer token cross-origin
  });

  it("attaches the bearer token from localStorage", async () => {
    localStorage.setItem(TOKEN_KEY, "or-secret");
    fetchMock.mockResolvedValue(fakeResponse({ body: "{}" }));
    await request("/users/info");
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer or-secret");
  });

  it("omits Authorization when noAuth is set", async () => {
    localStorage.setItem(TOKEN_KEY, "or-secret");
    fetchMock.mockResolvedValue(fakeResponse({ body: "{}" }));
    await request("/health_check", { noAuth: true });
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["Authorization"]).toBeUndefined();
  });

  it("clears the stored token on 401", async () => {
    localStorage.setItem(TOKEN_KEY, "or-secret");
    fetchMock.mockResolvedValue(fakeResponse({ status: 401 }));
    await expect(request("/users/info")).rejects.toMatchObject({ status: 401 });
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("tolerates an empty 200 body (resolves undefined, no JSON parse crash)", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: "" }));
    await expect(request("/partition/x")).resolves.toBeUndefined();
  });
});

describe("ApiError message parsing", () => {
  it("uses the FastAPI {detail} envelope", () => {
    expect(new ApiError(422, { detail: "bad input" }).message).toBe("bad input");
  });

  it("uses the domain {error:{message}} envelope", () => {
    expect(new ApiError(500, { error: { message: "boom", type: "x" } }).message).toBe("boom");
  });

  it("falls back to 'HTTP <status>' for unrecognized / non-object bodies", () => {
    expect(new ApiError(503, { something: "else" }).message).toBe("HTTP 503");
    expect(new ApiError(503, null).message).toBe("HTTP 503");
    expect(new ApiError(503, "plain string").message).toBe("HTTP 503");
  });

  it("retains status, body and name", () => {
    const e = new ApiError(404, { detail: "nope" });
    expect(e.status).toBe(404);
    expect(e.body).toEqual({ detail: "nope" });
    expect(e.name).toBe("ApiError");
  });
});
