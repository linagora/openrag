import { request } from "./client";
import {
  listPartitions,
  listPartitionMembers,
  addPartitionMember,
  removePartitionMember,
  updatePartitionMemberRole,
  type PartitionRole,
} from "./partitions";

// OpenRag users are provisioned against an external IdP: login matches a person
// to a row by `external_user_id == sub`. Identity (display_name, email) is
// IdP-owned; the admin only controls is_admin, file_quota, partition roles, and
// the user's bearer token (programmatic access).
export interface UserResponse {
  id: number;
  display_name: string | null;
  external_user_id: string | null;
  email: string | null;
  is_admin: boolean;
  file_quota: number | null;
  file_count?: number | null;
  created_at: string | null;
  /** Only returned by GET /users/{id}, not by the list endpoint. */
  memberships?: UserMembership[];
}

/** create and regenerate_token also return the one-time bearer token. */
export interface UserWithToken extends UserResponse {
  token: string;
}

export interface UserListResponse {
  users: UserResponse[];
}

export interface UserCreateRequest {
  display_name?: string;
  external_user_id?: string;
  email?: string;
  is_admin?: boolean;
  file_quota?: number | null;
}

// PATCH semantics differ from create: omitting a field leaves it unchanged,
// while sending an explicit `null` clears the nullable identity columns (e.g.
// clearing external_user_id returns the user to the token-only state). Create
// can't express null, so this is a standalone type rather than an extension.
export interface UserUpdateRequest {
  display_name?: string | null;
  external_user_id?: string | null;
  email?: string | null;
  is_admin?: boolean;
  file_quota?: number | null;
}

/**
 * Resolve a user's *effective* file quota from the raw `file_quota` column,
 * mirroring the backend rule: admins are unlimited; `null` inherits the global
 * default (`DEFAULT_FILE_QUOTA`, from GET /config); a resolved value `< 0` is
 * unlimited. Returns `"unknown"` when the global default hasn't loaded yet.
 */
export function effectiveQuota(
  fileQuota: number | null | undefined,
  isAdmin: boolean,
  globalDefault: number | null | undefined,
): number | "unlimited" | "unknown" {
  if (isAdmin) return "unlimited";
  let q: number | null = fileQuota ?? null;
  if (q == null) q = globalDefault ?? null;
  if (q == null) return "unknown";
  return q < 0 ? "unlimited" : q;
}

const BASE = "/users";

export function listUsers() {
  return request<UserListResponse>(`${BASE}/`);
}

export function getUser(id: string | number) {
  return request<UserResponse>(`${BASE}/${id}`);
}

/** Pre-provision a user: create the row before their first SSO login so the
 *  `external_user_id == sub` match succeeds (required in strict mode; harmless
 *  when OIDC_AUTO_PROVISION_LOGIN is on). Returns the one-time token. */
export function createUser(data: UserCreateRequest) {
  return request<UserWithToken>(`${BASE}/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateUser(id: string | number, data: UserUpdateRequest) {
  return request<UserResponse>(`${BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteUser(id: string | number) {
  return request<void>(`${BASE}/${id}`, { method: "DELETE" });
}

// The backend never deletes the default partition: a user who owns it only
// loses their membership, and the default admin becomes its owner.
const DEFAULT_PARTITION = "default";

/** Confirmation text for deleting a user. Deleting a user deletes every
 *  partition where they hold the owner role (even if others own it too), with
 *  its documents. Pass `memberships` to name those partitions; without them the
 *  text stays generic. */
export function deleteUserDescription(label: string, memberships?: UserMembership[]): string {
  const intro = `Permanently delete "${label}"?`;
  const outro = "They lose access to every other partition, and their token stops working.";
  if (!memberships) {
    return `${intro} Every partition they own is deleted too, with all its documents (the default partition is kept). ${outro}`;
  }
  const owned = memberships.filter((m) => m.role === "owner").map((m) => m.partition);
  const deleted = owned.filter((p) => p !== DEFAULT_PARTITION);
  const parts = [intro];
  if (deleted.length === 0) {
    parts.push("They own no partitions that would be deleted.");
  } else if (deleted.length === 1) {
    parts.push(`This also deletes the partition they own, with all its documents: ${deleted[0]}.`);
  } else {
    parts.push(
      `This also deletes the ${deleted.length} partitions they own, with all their documents: ${deleted.join(", ")}.`,
    );
  }
  if (owned.includes(DEFAULT_PARTITION)) {
    parts.push("The default partition is kept, and the default admin becomes its owner.");
  }
  parts.push(outro);
  return parts.join(" ");
}

// One rotating bearer token per user; regenerating invalidates the old one.
export function regenerateUserToken(id: string | number) {
  return request<UserWithToken>(`${BASE}/${id}/regenerate_token`, { method: "POST" });
}

// ── Partition memberships ────────────────────────────────────────────────────
// OpenRag membership is partition-scoped — there is no `/users/{id}/partitions`
// endpoint, so a user's memberships are derived by scanning partitions. Mutations
// go straight to the partition membership endpoints.

export interface UserMembership {
  partition: string;
  role: PartitionRole;
  added_at: string | null;
}

export async function listUserMemberships(userId: number): Promise<UserMembership[]> {
  const { partitions } = await listPartitions();
  const rows = await Promise.all(
    partitions.map(async (p) => {
      try {
        const { members } = await listPartitionMembers(p.name);
        const m = members.find((mm) => mm.user_id === userId);
        return m ? { partition: p.name, role: m.role as PartitionRole, added_at: m.added_at } : null;
      } catch {
        return null;
      }
    }),
  );
  return rows.filter((r): r is UserMembership => r !== null);
}

export function grantMembership(partition: string, userId: number, role: PartitionRole) {
  return addPartitionMember(partition, userId, role);
}

export function revokeMembership(partition: string, userId: number) {
  return removePartitionMember(partition, userId);
}

export function changeMembershipRole(partition: string, userId: number, role: PartitionRole) {
  return updatePartitionMemberRole(partition, userId, role);
}
