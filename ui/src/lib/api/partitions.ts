import { request } from "./client";

export interface PartitionResponse {
  name: string;
  exists: boolean;
  description: string;
  embedder: string;
  dimension: number;
  collection_name: string | null;
  chat_history_depth: number;
  chat_llm: string | null;
  document_count: number;
  indexation_preset: string;
  retrieval_preset: string;
  indexation_pipeline: Record<string, unknown>;
  retrieval_pipeline: Record<string, unknown>;
  created_at: string | null;
}

export interface PartitionListResponse {
  partitions: PartitionResponse[];
}

export interface CreatePartitionRequest {
  name: string;
  description?: string;
  embedder?: string;
  indexation_preset?: string;
  retrieval_preset?: string;
  collection_name?: string;
  chat_history_depth?: number;
  chat_llm?: string | null;
}

export interface UpdatePartitionRequest {
  description?: string;
  indexation_preset?: string;
  retrieval_preset?: string;
  collection_name?: string;
  chat_history_depth?: number;
  chat_llm?: string | null;
}

export interface UserPartitionResponse {
  user_id: string;
  partition: string;
  role: string;
  created_at: string;
}

export interface PartitionUsersResponse {
  partition: string;
  users: UserPartitionResponse[];
}

const BASE = "/api/v1/admin/partitions";

export function listPartitions() {
  return request<PartitionListResponse>(BASE);
}

export function searchPartitions(query: string, limit = 20) {
  const params = new URLSearchParams();
  if (query) params.set("search", query);
  params.set("limit", String(limit));
  return request<PartitionListResponse>(`${BASE}?${params}`);
}

export function getPartition(name: string) {
  return request<PartitionResponse>(`${BASE}/${name}`);
}

export function createPartition(data: CreatePartitionRequest) {
  return request<PartitionResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updatePartition(name: string, data: UpdatePartitionRequest) {
  return request<PartitionResponse>(`${BASE}/${name}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function deletePartition(name: string) {
  return request<{ deleted: string }>(`${BASE}/${name}`, { method: "DELETE" });
}

export function listPartitionUsers(partition: string) {
  return request<PartitionUsersResponse>(`${BASE}/${partition}/users`);
}

export interface UserPartitionDetailResponse {
  name: string;
  /** Legacy field; UI falls back to `name` when absent from the response. */
  internal_name?: string;
  description: string;
  document_count: number;
  role: string; // "OWNER" | "READER"
  created_at: string | null;
}

export interface UserPartitionListResponse {
  partitions: UserPartitionDetailResponse[];
}

export function listCurrentUserPartitions() {
  return request<UserPartitionListResponse>("/api/v1/user/partitions");
}

export function createUserPartition(data: CreatePartitionRequest) {
  return request<PartitionResponse>("/api/v1/user/partitions", {
    method: "POST",
    body: JSON.stringify(data),
  });
}
