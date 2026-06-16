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

export type UserUpdateRequest = UserCreateRequest;

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
