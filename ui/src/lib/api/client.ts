// Default to same-origin (empty base → relative fetch, proxied by nginx). A
// missing env must fail closed: never fall back to an absolute host, which would
// send the bearer token cross-origin. To target a remote API in dev, set
// VITE_API_BASE_URL explicitly.
const apiBase = () => import.meta.env.VITE_API_BASE_URL ?? "";

export function apiUrl(path: string): string {
  const base = apiBase();
  if (!base) return path;

  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base.replace(/\/+$/, "")}${normalizedPath}`;
}

// OpenRag bearer token (the user's `or-…` token). Sent as Authorization for
// token-mode auth; in OIDC mode the same-origin session cookie is used instead.
export const TOKEN_KEY = "openrag_token";

export class ApiError extends Error {
  status: number;
  body: unknown;
  code?: string;

  constructor(status: number, body: unknown) {
    // API uses two envelopes: {detail: "..."} for FastAPI validation errors,
    // and {error: {message, type, code, request_id}} for domain errors.
    let raw = `HTTP ${status}`;
    if (typeof body === "object" && body !== null) {
      const b = body as Record<string, unknown>;
      if (typeof b.detail === "string") {
        raw = b.detail;
      } else if (b.error && typeof b.error === "object") {
        const e = b.error as Record<string, unknown>;
        if (typeof e.message === "string") raw = e.message;
      }
    }
    // Domain errors arrive as "[CODE]: message" (the legacy detail contract).
    // Strip the machine code from the human-facing message so toasts read
    // cleanly, and keep it on `.code` for programmatic use.
    const match = /^\[([A-Z0-9_]+)\]:\s*([\s\S]*)$/.exec(raw);
    super(match ? match[2] : raw);
    this.name = "ApiError";
    this.status = status;
    this.code = match ? match[1] : undefined;
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

  const res = await fetch(apiUrl(path), {
    ...fetchOptions,
    headers,
    // Send the OIDC `openrag_session` cookie. A no-op for the same-origin
    // default, but required for a browser-direct build (absolute
    // VITE_API_BASE_URL): without it the cross-origin cookie is dropped and
    // OIDC users are treated as unauthenticated. The API sets
    // Access-Control-Allow-Credentials, so list the UI origin in
    // CORS_EXTRA_ORIGINS when going cross-origin.
    credentials: "include",
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
