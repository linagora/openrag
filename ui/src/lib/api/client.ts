const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

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
  const token = localStorage.getItem("access_token");
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
    // Try token refresh
    const refreshToken = localStorage.getItem("refresh_token");
    if (refreshToken && !path.includes("/auth/")) {
      try {
        const refreshRes = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (refreshRes.ok) {
          const data = await refreshRes.json();
          localStorage.setItem("access_token", data.access_token);
          // Retry original request with new token
          headers["Authorization"] = `Bearer ${data.access_token}`;
          const retry = await fetch(`${API_BASE}${path}`, { ...fetchOptions, headers });
          if (retry.ok) {
            if (retry.status === 204) return undefined as T;
            return retry.json();
          }
        }
      } catch {
        // refresh failed
      }
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      // BASE_URL is "/app/" so this resolves to "/app/login" (matches the SPA
      // basename in router.tsx; plain "/login" 404s on nginx).
      window.location.href = `${import.meta.env.BASE_URL}login`;
    }
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

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/")) {
    return (await res.text()) as T;
  }
  return res.json();
}
