import { request } from "./client";

// OpenRag indexing — the WRITE side of the per-partition file model, mounted
// under `/indexer`. Files are addressed by a CLIENT-SUPPLIED `file_id` scoped
// to a partition; there is no flat `/documents` collection and no batch endpoint
// (multi-file upload is a client-side loop). Verified vs routers/admin/indexing.py:
//   GET    /indexer/supported/types                 → { extensions, mimetypes }
//   POST   /indexer/partition/{p}/file/{id}         upload+index (multipart) → 201 { task_status_url }
//   PUT    /indexer/partition/{p}/file/{id}         replace (multipart)      → 202 { task_status_url }
//   PATCH  /indexer/partition/{p}/file/{id}         update metadata only     → 200 { message }
//   DELETE /indexer/partition/{p}/file/{id}         delete                   → 204
//   POST   /indexer/partition/{p}/file/{id}/copy    copy from another partition (multipart) → 201
//
// Upload/replace are multipart: a `file` part + a `metadata` form field holding
// a JSON string (NOT a JSON body); `workspace_ids` is an optional JSON-array string.

const I = "/indexer";
const enc = encodeURIComponent;

export interface SupportedTypes {
  extensions: string[];
  mimetypes: string[];
}

/** Upload and replace return a pollable task URL — feed the id to jobs.ts. */
export interface TaskAccepted {
  task_status_url: string;
}

export function getSupportedTypes(): Promise<SupportedTypes> {
  return request<SupportedTypes>(`${I}/supported/types`);
}

function fileForm(file: File, metadata?: Record<string, unknown>, workspaceIds?: string[]): FormData {
  const form = new FormData();
  form.append("file", file);
  if (metadata !== undefined) form.append("metadata", JSON.stringify(metadata));
  if (workspaceIds !== undefined) form.append("workspace_ids", JSON.stringify(workspaceIds));
  return form;
}

/** Generate a unique, URL-safe file_id for a new upload. */
export function newFileId(): string {
  // crypto.randomUUID() only exists in secure contexts (HTTPS/localhost); over
  // plain HTTP it's undefined. crypto.getRandomValues works everywhere, so fall
  // back to a manual UUID v4.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40; // version 4
  b[8] = (b[8] & 0x3f) | 0x80; // variant
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0"));
  return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
}

/**
 * Upload and index a new file. `fileId` is chosen by the caller (unique within
 * the partition; 409 if it already exists). Returns 201 + a task URL to poll.
 */
export function uploadFile(
  partition: string,
  fileId: string,
  file: File,
  metadata?: Record<string, unknown>,
  workspaceIds?: string[],
): Promise<TaskAccepted> {
  return request<TaskAccepted>(`${I}/partition/${enc(partition)}/file/${enc(fileId)}`, {
    method: "POST",
    body: fileForm(file, metadata, workspaceIds),
  });
}

/** Replace an existing file, preserving its `file_id`. Returns 202 + task URL. */
export function replaceFile(
  partition: string,
  fileId: string,
  file: File,
  metadata?: Record<string, unknown>,
): Promise<TaskAccepted> {
  return request<TaskAccepted>(`${I}/partition/${enc(partition)}/file/${enc(fileId)}`, {
    method: "PUT",
    body: fileForm(file, metadata),
  });
}

/** Update file metadata in place (no re-upload). A `partition` key MOVES the file. */
export function updateFileMetadata(
  partition: string,
  fileId: string,
  metadata: Record<string, unknown>,
): Promise<{ message: string }> {
  const form = new FormData();
  form.append("metadata", JSON.stringify(metadata));
  return request<{ message: string }>(`${I}/partition/${enc(partition)}/file/${enc(fileId)}`, {
    method: "PATCH",
    body: form,
  });
}

export function deleteFile(partition: string, fileId: string): Promise<void> {
  return request<void>(`${I}/partition/${enc(partition)}/file/${enc(fileId)}`, { method: "DELETE" });
}

/** Copy a file into `partition` as `fileId` from `sourcePartition`/`sourceFileId`. */
export function copyFile(
  partition: string,
  fileId: string,
  sourcePartition: string,
  sourceFileId: string,
  metadata?: Record<string, unknown>,
): Promise<{ message: string }> {
  const form = new FormData();
  form.append("source_partition", sourcePartition);
  form.append("source_file_id", sourceFileId);
  if (metadata !== undefined) form.append("metadata", JSON.stringify(metadata));
  return request<{ message: string }>(`${I}/partition/${enc(partition)}/file/${enc(fileId)}/copy`, {
    method: "POST",
    body: form,
  });
}
