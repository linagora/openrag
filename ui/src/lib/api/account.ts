import { request } from "./client";

// OpenRag has no passwords and no multi-key API. A user has ONE bearer token
// (`or-…`), rotated via regenerate. Identity/sign-in is owned by the IdP (SSO).

export interface MyInfo {
  id: number;
  display_name: string | null;
  is_admin: boolean;
  chainlit_enabled?: boolean;
  file_quota: number | null;
  file_count?: number;
  indexed_files?: number;
  pending_files?: number;
  total_files?: number;
  email?: string | null;
  external_user_id?: string | null;
}

export interface CurrentAuthInfo {
  user_id: number | null;
  email?: string | null;
  auth_method: "token" | "oidc" | string;
  session_expires_at?: string | null;
}

/** Current authenticated user (from the bearer token / session). */
export function getMyInfo() {
  return request<MyInfo>("/users/info");
}

/** Current authentication method resolved by the backend middleware. */
export function getCurrentAuthInfo() {
  return request<CurrentAuthInfo>("/auth/me");
}

/**
 * Regenerate the current user's API token. The new token is returned ONCE and
 * the old one is invalidated immediately. Self-regeneration needs the user id,
 * which we resolve from /users/info.
 */
export async function regenerateMyToken(): Promise<{ token: string }> {
  const me = await getMyInfo();
  return request<{ token: string }>(`/users/${me.id}/regenerate_token`, { method: "POST" });
}
