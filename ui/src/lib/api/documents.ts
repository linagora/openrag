import { request } from "./client";

export interface DocumentResponse {
  id: string;
  filename: string;
  content_type: string;
  partition: string;
  chunk_count: number;
  file_size_bytes: number | null;
  status: string;
  error_message: string | null;
  retry_count: number;
  job_id: string | null;
  indexation_config: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  indexed_at: string | null;
}

export interface DocumentListResponse {
  documents: DocumentResponse[];
  total: number;
  offset: number;
  limit: number;
}

export interface CopyDocumentRequest {
  target_partition: string;
}

const BASE = "/api/v1/admin/documents";

export function listDocuments(params?: {
  partition?: string;
  status?: string;
  offset?: number;
  limit?: number;
}) {
  const search = new URLSearchParams();
  if (params?.partition) search.set("partition", params.partition);
  if (params?.status) search.set("status", params.status);
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<DocumentListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getDocument(id: string) {
  return request<DocumentResponse>(`${BASE}/${id}`);
}

export function replaceDocument(id: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return request<DocumentResponse>(`${BASE}/${id}`, {
    method: "PUT",
    body: form,
  });
}

export function copyDocument(id: string, targetPartition: string) {
  return request<DocumentResponse>(`${BASE}/${id}/copy`, {
    method: "POST",
    body: JSON.stringify({ target_partition: targetPartition }),
  });
}

export function deletePartitionDocuments(partition: string) {
  return request<{ partition: string; documents_deleted: number }>(
    `${BASE}/partition/${partition}`,
    { method: "DELETE" },
  );
}

export function deleteDocument(id: string) {
  return request<{ deleted: boolean; document_id: string }>(`${BASE}/${id}`, {
    method: "DELETE",
  });
}

export interface EntityItem {
  name: string;
  type: string;
}

export interface TopicItem {
  tag: string;
  confidence: number;
}

export interface ChunkResponse {
  id: string;
  document_id: string;
  chunk_index: number;
  chunk_type: string;
  text: string;
  content: string;
  context: string | null;
  header: string | null;
  page_number: number | null;
  token_count: number;
  metadata: Record<string, unknown>;
  entities: EntityItem[];
  topics: TopicItem[];
}

export interface ChunkListResponse {
  chunks: ChunkResponse[];
  total: number;
}

export function getDocumentChunks(documentId: string) {
  return request<ChunkListResponse>(`${BASE}/${documentId}/chunks`);
}
