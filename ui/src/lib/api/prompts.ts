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

const BASE = "/api/v1/admin/prompts";

// Prompts is a dropped feature; presets only reads the prompt list (to pick a
// chat prompt). The CRUD/partition-assignment surface was removed — re-add from
// git history if a prompt-management page is reinstated.
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
