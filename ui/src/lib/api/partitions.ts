import { request } from "./client";

// OpenRag partitions — mounted at `/partition`. Verified vs
// api/schemas/admin/partition_schemas.py + routers/admin/partitions.py:
//   GET    /partition/                      list (scoped) → { partitions: [{ partition, created_at, document_count }] }
//   GET    /partition/{p}                   LIST FILES   → { files: [...] }
//   GET    /partition/{p}/config            resolved config (PartitionDetailResponse, incl. document_count)
//   POST   /partition/{p}                   create (name in path, NO body; caller becomes owner) → 201
//   PATCH  /partition/{p}                   update config → PartitionDetailResponse
//   DELETE /partition/{p}                   delete → 204
//   GET    /partition/{p}/users             members → { members: [{ user_id, role, added_at }] }
//   POST   /partition/{p}/users             add member   (multipart: user_id, role)
//   PATCH  /partition/{p}/users/{user_id}   change role  (multipart: role)
//   DELETE /partition/{p}/users/{user_id}   remove member
//
// NOTE (transition): several exports below keep their pre-OpenRag names/shapes
// so the not-yet-migrated pages (partitions/list+detail, documents/*, overview)
// keep compiling. The list-row `name` field aliases the real `partition` key.
// These compat shims are removed as each page is wired to the clean API.

const P = "/partition";
const enc = encodeURIComponent;

export type PartitionRole = "viewer" | "editor" | "owner";

/** A partition row. Real keys from `GET /partition/` are `partition`,
 *  `created_at`, `document_count` (+ `role` on the user-scoped path). For the
 *  migration this keeps the old rich shape (`name` aliases `partition`; config
 *  fields are filled with defaults by `_toRow`) so not-yet-wired pages compile.
 *  Trimmed to the real fields as each consumer is migrated. */
export interface PartitionResponse {
  partition: string;
  name: string;
  internal_name?: string;
  role: PartitionRole | string;
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

/** GET /partition/{p}/config — PartitionDetailResponse. */
export interface PartitionConfig {
  name: string;
  description: string;
  embedder: string;
  indexation_preset: string;
  retrieval_preset: string;
  indexation_pipeline: Record<string, unknown>;
  retrieval_pipeline: Record<string, unknown>;
  dimension: number;
  created_at: string;
  document_count: number;
  chat_history_depth: number;
  chat_llm: string | null;
}

export interface UpdatePartitionRequest {
  description?: string;
  embedder?: string;
  indexation_preset?: string;
  retrieval_preset?: string;
  chat_history_depth?: number;
  chat_llm?: string | null;
  /** Accepted for compat but never sent (server has no such column). */
  collection_name?: string;
}

export interface CreatePartitionRequest extends UpdatePartitionRequest {
  name: string;
}

const _PATCH_FIELDS = [
  "description",
  "embedder",
  "indexation_preset",
  "retrieval_preset",
  "chat_history_depth",
  "chat_llm",
] as const;

function _toRow(r: Record<string, unknown>): PartitionResponse {
  const partition = (r.partition ?? r.name) as string;
  return {
    // defaults for config-only fields not present on a list row
    role: "",
    exists: true,
    description: "",
    embedder: "",
    dimension: 0,
    collection_name: null,
    chat_history_depth: 0,
    chat_llm: null,
    indexation_preset: "",
    retrieval_preset: "",
    indexation_pipeline: {},
    retrieval_pipeline: {},
    ...r,
    partition,
    name: partition,
    document_count: (r.document_count as number) ?? 0,
    created_at: (r.created_at as string | null) ?? null,
  };
}

// ── Partitions ───────────────────────────────────────────────────────────────

export async function listPartitions(): Promise<PartitionListResponse> {
  const res = await request<{ partitions: Record<string, unknown>[] }>(`${P}/`);
  return { partitions: (res.partitions ?? []).map(_toRow) };
}

/** The user-scoped list IS `GET /partition/` (server filters by membership). */
export const listCurrentUserPartitions = listPartitions;

export async function searchPartitions(query: string, _limit = 20): Promise<PartitionListResponse> {
  const { partitions } = await listPartitions();
  const q = query.trim().toLowerCase();
  return { partitions: q ? partitions.filter((p) => p.name.toLowerCase().includes(q)) : partitions };
}

export function getPartitionConfig(name: string): Promise<PartitionConfig> {
  return request<PartitionConfig>(`${P}/${enc(name)}/config`);
}

/** Compat: previously a rich partition object — now the resolved config (carries name/description/document_count). */
export async function getPartition(name: string): Promise<PartitionResponse> {
  const c = await getPartitionConfig(name);
  return { ..._toRow(c as unknown as Record<string, unknown>), ...c, partition: c.name, name: c.name };
}

/** Create a partition. Name goes in the path (no body); the caller becomes
 *  owner. Any extra config is applied via a follow-up PATCH. */
export async function createPartition(data: CreatePartitionRequest): Promise<PartitionConfig> {
  const { name } = data;
  await request<void>(`${P}/${enc(name)}`, { method: "POST" });
  const hasConfig = _PATCH_FIELDS.some((k) => data[k] !== undefined && data[k] !== "");
  if (!hasConfig) return getPartitionConfig(name);
  try {
    return await updatePartition(name, data);
  } catch (e) {
    // The partition was created empty above; if applying its config fails, roll it
    // back so we don't leave an orphaned, half-configured partition (which would
    // also 409 on retry). Best-effort delete — surface the original error.
    await deletePartition(name).catch(() => {});
    throw e;
  }
}

/** Compat alias — OpenRag has no separate user-partition create path. */
export const createUserPartition = createPartition;

export function updatePartition(name: string, data: UpdatePartitionRequest): Promise<PartitionConfig> {
  const body: Record<string, unknown> = {};
  for (const k of _PATCH_FIELDS) {
    if (data[k] !== undefined) body[k] = data[k];
  }
  return request<PartitionConfig>(`${P}/${enc(name)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deletePartition(name: string): Promise<void> {
  return request<void>(`${P}/${enc(name)}`, { method: "DELETE" });
}

// ── Files (read side; adopted by the documents slice) ─────────────────────────

export interface PartitionFile {
  file_id: string;
  partition: string;
  link: string;
  filename?: string;
  [key: string]: unknown;
}

export function listPartitionFiles(name: string, limit?: number): Promise<{ files: PartitionFile[] }> {
  const qs = limit != null ? `?limit=${limit}` : "";
  return request<{ files: PartitionFile[] }>(`${P}/${enc(name)}${qs}`);
}

// ── Members (partition-centric; adopted by partitions/detail in a later commit) ─

export interface PartitionMember {
  user_id: number;
  role: PartitionRole;
  added_at: string | null;
}

export interface PartitionMemberCandidate {
  user_id: number;
  display_name: string | null;
}

export interface PartitionMemberCandidatePage {
  candidates: PartitionMemberCandidate[];
  limit: number;
  has_more: boolean;
  next_cursor: number | null;
}

interface ListPartitionMemberCandidatesOptions {
  search: string;
  cursor?: number;
  limit?: number;
}

export function listPartitionMembers(name: string): Promise<{ members: PartitionMember[] }> {
  return request<{ members: PartitionMember[] }>(`${P}/${enc(name)}/users`);
}

export function listPartitionMemberCandidates(
  name: string,
  options: ListPartitionMemberCandidatesOptions,
): Promise<PartitionMemberCandidatePage> {
  const query = new URLSearchParams();
  query.set("search", options.search.trim());
  if (options.cursor !== undefined) query.set("cursor", String(options.cursor));
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  return request<PartitionMemberCandidatePage>(
    `${P}/${enc(name)}/users/candidates?${query.toString()}`,
  );
}

export function addPartitionMember(name: string, userId: number, role: PartitionRole): Promise<void> {
  const form = new FormData();
  form.append("user_id", String(userId));
  form.append("role", role);
  return request<void>(`${P}/${enc(name)}/users`, { method: "POST", body: form });
}

export function updatePartitionMemberRole(name: string, userId: number, role: PartitionRole): Promise<void> {
  const form = new FormData();
  form.append("role", role);
  return request<void>(`${P}/${enc(name)}/users/${userId}`, { method: "PATCH", body: form });
}

export function removePartitionMember(name: string, userId: number): Promise<void> {
  return request<void>(`${P}/${enc(name)}/users/${userId}`, { method: "DELETE" });
}

// Compat type aliases for the user-scoped list (overview / documents / list page).
export type UserPartitionDetailResponse = PartitionResponse;
export type UserPartitionListResponse = PartitionListResponse;
