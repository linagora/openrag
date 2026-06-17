const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

// OpenRag bearer token (the user's `or-…` token). Sent as Authorization for
// token-mode auth; in OIDC mode the same-origin session cookie is used instead.
export const TOKEN_KEY = "openrag_token";

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    // API uses two envelopes: {detail: "..."} for FastAPI validation errors,
    // and {error: {message, type, code, request_id}} for domain errors.
    let msg = `HTTP ${status}`;
    if (typeof body === "object" && body !== null) {
      const b = body as Record<string, unknown>;
      if (typeof b.detail === "string") {
        msg = b.detail;
      } else if (b.error && typeof b.error === "object") {
        const e = b.error as Record<string, unknown>;
        if (typeof e.message === "string") msg = e.message;
      }
    }
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export async function request<T>(
  path: string,
  options?: RequestInit & { noAuth?: boolean },
): Promise<T> {
  const { noAuth, ...fetchOptions } = options ?? {};
  const token = localStorage.getItem(TOKEN_KEY);
  const headers: Record<string, string> = {};

  if (token && !noAuth) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  // Merge caller headers
  if (fetchOptions.headers) {
    const h = fetchOptions.headers as Record<string, string>;
    Object.assign(headers, h);
  }

  // Don't set Content-Type for FormData (browser sets multipart boundary)
  if (!(fetchOptions.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    headers,
  });

  if (res.status === 401) {
    // Stale/absent credentials. Drop any stored token so the next app load lands
    // on the login screen (ProtectedRoute redirects unauthenticated users). The
    // auth probe (/users/info) catches this to render the login page.
    localStorage.removeItem(TOKEN_KEY);
    throw new ApiError(401, { detail: "Unauthorized" });
  }

  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = { detail: res.statusText };
    }
    throw new ApiError(res.status, body);
  }

  if (res.status === 204) return undefined as T;

  // Read the body once and tolerate empty responses — several endpoints return
  // an empty 200/201 (e.g. POST /partition/{name}), which would otherwise crash
  // res.json() with "Unexpected end of JSON input".
  const text = await res.text();
  if (!text) return undefined as T;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/")) return text as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as T;
  }
}
