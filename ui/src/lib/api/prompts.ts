import { request } from "./client";

export interface PromptUsedBy {
  count: number;
  partitions: string[];
}

export interface PromptResponse {
  id: string;
  prompt_type: string;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
  used_by: PromptUsedBy | null;
}

export interface PromptListResponse {
  prompts: PromptResponse[];
  offset: number;
  limit: number;
}

export interface CreatePromptRequest {
  prompt_type: string;
  name?: string;
  content: string;
  is_default?: boolean;
}

export interface UpdatePromptRequest {
  name?: string;
  content?: string;
  is_default?: boolean;
}

export interface PromptBriefResponse {
  id: string;
  name: string;
  prompt_type: string;
}

export interface PartitionPromptMap {
  partition: string;
  assignments: Record<string, PromptBriefResponse | null>;
}

export interface AssignPartitionPromptRequest {
  prompt_id: string;
}

const BASE = "/api/v1/admin/prompts";

export function listPrompts(params?: {
  prompt_type?: string;
  offset?: number;
  limit?: number;
}) {
  const search = new URLSearchParams();
  if (params?.prompt_type) search.set("prompt_type", params.prompt_type);
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<PromptListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getPrompt(id: string) {
  return request<PromptResponse>(`${BASE}/${id}`);
}

export function createPrompt(data: CreatePromptRequest) {
  return request<PromptResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updatePrompt(id: string, data: UpdatePromptRequest) {
  return request<PromptResponse>(`${BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deletePrompt(id: string) {
  return request<void>(`${BASE}/${id}`, { method: "DELETE" });
}

export function setPromptDefault(id: string) {
  return request<PromptResponse>(`${BASE}/${id}/default`, { method: "PUT" });
}

export function getPartitionPrompts(partitionName: string) {
  return request<PartitionPromptMap>(
    `/api/v1/admin/partitions/${encodeURIComponent(partitionName)}/prompts`
  );
}

export function assignPartitionPrompt(partitionName: string, promptType: string, promptId: string) {
  return request<void>(
    `/api/v1/admin/partitions/${encodeURIComponent(partitionName)}/prompts/${promptType}`,
    { method: "PUT", body: JSON.stringify({ prompt_id: promptId }) }
  );
}

export function unassignPartitionPrompt(partitionName: string, promptType: string) {
  return request<void>(
    `/api/v1/admin/partitions/${encodeURIComponent(partitionName)}/prompts/${promptType}`,
    { method: "DELETE" }
  );
}
