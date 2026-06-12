import { request } from "./client";

export interface UserResponse {
  id: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface UserListResponse {
  users: UserResponse[];
  offset: number;
  limit: number;
}

export interface CreateUserRequest {
  email: string;
  password: string;
  display_name?: string;
  role?: string;
}

export interface UpdateUserRequest {
  email?: string;
  password?: string;
  display_name?: string;
  role?: string;
  is_active?: boolean;
}

export interface ApiKeyResponse {
  id: string;
  key_prefix: string;
  name: string;
  is_active: boolean;
  created_at: string;
  expires_at: string | null;
}

export interface ApiKeyCreatedResponse extends ApiKeyResponse {
  raw_key: string;
}

export interface UserPartitionResponse {
  user_id: string;
  partition: string;
  role: string;
  created_at: string;
}

const BASE = "/api/v1/admin/users";

export function listUsers(params?: { offset?: number; limit?: number }) {
  const search = new URLSearchParams();
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<UserListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getUser(id: string) {
  return request<UserResponse>(`${BASE}/${id}`);
}

export function createUser(data: CreateUserRequest) {
  return request<UserResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateUser(id: string, data: UpdateUserRequest) {
  return request<UserResponse>(`${BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteUser(id: string) {
  return request<void>(`${BASE}/${id}`, { method: "DELETE" });
}

// API token — OpenRag gives each user ONE bearer token, rotated here (admin or self).
// Real endpoint: POST /users/{id}/regenerate_token → { token } (shown once).
export function regenerateUserToken(userId: string) {
  return request<{ token: string }>(`${BASE}/${userId}/regenerate_token`, { method: "POST" });
}

// API Keys
export function createApiKey(userId: string, name?: string) {
  return request<ApiKeyCreatedResponse>(`${BASE}/${userId}/api-keys`, {
    method: "POST",
    body: JSON.stringify({ name: name || "" }),
  });
}

export function revokeApiKey(userId: string, keyId: string) {
  return request<void>(`${BASE}/${userId}/api-keys/${keyId}`, {
    method: "DELETE",
  });
}

// Partition assignments
export function listUserPartitions(userId: string) {
  return request<UserPartitionResponse[]>(`${BASE}/${userId}/partitions`);
}

export function assignPartition(
  userId: string,
  partition: string,
  role: string = "reader",
) {
  return request<UserPartitionResponse>(`${BASE}/${userId}/partitions`, {
    method: "POST",
    body: JSON.stringify({ partition, role }),
  });
}

export function removePartition(userId: string, partition: string) {
  return request<void>(`${BASE}/${userId}/partitions/${partition}`, {
    method: "DELETE",
  });
}

// Bulk import
export interface ImportedUserRow {
  email: string;
  display_name: string;
  role: string;
  temporary_password: string;
  partitions: string[];
}

export interface ImportErrorRow {
  row: number;
  email: string | null;
  errors: string[];
}

export interface BulkImportResponse {
  created: ImportedUserRow[];
  count: number;
}

export function importUsers(file: File): Promise<BulkImportResponse> {
  const form = new FormData();
  form.append("file", file);
  return request<BulkImportResponse>(`${BASE}/import`, {
    method: "POST",
    body: form,
  });
}
