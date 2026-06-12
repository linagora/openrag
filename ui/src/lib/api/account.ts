import { request } from "./client";

// OpenRag has no passwords and no multi-key API. A user has ONE bearer token
// (`or-…`), rotated via regenerate. This module exposes only that.

const BASE = "/api/v1/user/account";

/**
 * Regenerate the current user's API token. The new token is returned ONCE;
 * the old one is invalidated immediately.
 * NOTE: in the API-wiring phase this targets OpenRag's
 * `POST /users/{id}/regenerate_token` (id from `/users/info`). The mock stands in for now.
 */
export function regenerateMyToken() {
  return request<{ token: string }>(`${BASE}/regenerate-token`, { method: "POST" });
}
