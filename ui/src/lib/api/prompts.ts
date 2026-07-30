import { request } from "./client";

// OpenRag prompt library (admin-only). Mounted at `/prompts`.
// Shapes verified against openrag/api/schemas/admin/prompt_schemas.py + routers/admin/prompts.py:
//   GET    /prompts/                 list (bare array; optional ?prompt_type=&offset=&limit=)
//   POST   /prompts/                 create → 201
//   GET    /prompts/{id}             get one
//   PATCH  /prompts/{id}             update (name/content/is_default)
//   PUT    /prompts/{id}/default     promote to default for its type
//   DELETE /prompts/{id}             delete → 204
//
// Prompts are *named*; a preset or partition selects one by naming it. There is
// no partition-assignment endpoint — selection lives in the preset/partition
// editors (see presets.tsx / partitions).

// The 8 managed prompt types (mirrors PromptTypeName on the backend).
export type PromptType =
  | "sys_prompt"
  | "query_contextualizer"
  | "chunk_contextualizer"
  | "image_captioning"
  | "hyde"
  | "multi_query"
  | "topic_tagger";

export interface PromptResponse {
  id: string;
  prompt_type: PromptType;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
  // Number of partitions that reference this prompt by name (directly for
  // generation prompts, transitively via preset for indexation/retrieval).
  // Only populated by listPrompts(); single-item responses default it to 0.
  used_by: number;
}

export interface CreatePromptRequest {
  prompt_type: PromptType;
  name: string;
  content: string;
  is_default?: boolean;
}

export interface UpdatePromptRequest {
  name?: string;
  content?: string;
  is_default?: boolean;
}

const BASE = "/prompts";
const enc = encodeURIComponent;

/** List library prompts (bare array). Optionally filter by type. */
export function listPrompts(params?: {
  prompt_type?: PromptType;
  offset?: number;
  limit?: number;
}) {
  const search = new URLSearchParams();
  if (params?.prompt_type) search.set("prompt_type", params.prompt_type);
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<PromptResponse[]>(`${BASE}/${qs ? `?${qs}` : ""}`);
}

/** Page size used when walking the whole library. The API caps `limit` at 500. */
const PAGE_SIZE = 200;

/** Fetch every library prompt, following offset pagination to the end.
 *
 *  The management page and every prompt picker need the complete library: a
 *  single capped request would silently hide prompts past the cap and report a
 *  partial count as if it were the total, leaving those prompts unmanageable
 *  and unselectable.
 */
export async function listAllPrompts(params?: { prompt_type?: PromptType }): Promise<PromptResponse[]> {
  const all: PromptResponse[] = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = await listPrompts({ ...params, offset, limit: PAGE_SIZE });
    all.push(...page);
    // A short page means the end; a full page means there may be more.
    if (page.length < PAGE_SIZE) return all;
  }
}

export function getPrompt(id: string) {
  return request<PromptResponse>(`${BASE}/${enc(id)}`);
}

export function createPrompt(data: CreatePromptRequest) {
  return request<PromptResponse>(`${BASE}/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updatePrompt(id: string, data: UpdatePromptRequest) {
  return request<PromptResponse>(`${BASE}/${enc(id)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

/** Promote a prompt to the default for its type. */
export function setPromptDefault(id: string) {
  return request<PromptResponse>(`${BASE}/${enc(id)}/default`, {
    method: "PUT",
  });
}

export function deletePrompt(id: string) {
  return request<void>(`${BASE}/${enc(id)}`, {
    method: "DELETE",
  });
}
