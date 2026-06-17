import { request } from "./client";
import { listPartitionFiles, type PartitionFile } from "./partitions";

// OpenRag documents — the READ side of the per-partition file model. There is
// no flat `/documents` collection: files live inside a partition and are
// listed/read through `/partition`. Writes (upload/replace/delete/copy) are in
// indexing.ts. Verified vs routers/admin/partitions.py + routers/user/extract.py:
//   GET /partition/{p}                       list files  → { files: [...] }   (partitions.ts)
//   GET /partition/{p}/file/{id}             detail+chunk links → { metadata, documents: [{ link }] }
//   GET /partition/{p}/chunks                all chunks (content) → { chunks: [{ link, content, metadata }] }
//   GET /extract/{extract_id}                one chunk's content → { page_content, metadata }

export { listPartitionFiles };
export type { PartitionFile };

const P = "/partition";
const enc = encodeURIComponent;

/** File detail: `metadata` is flattened from the file's first chunk row;
 *  `documents` are link-only stubs (one per chunk) pointing at /extract/{id}. */
export interface FileDetail {
  metadata: Record<string, unknown>;
  documents: { link: string }[];
}

/** A chunk WITH its text, from the partition-wide chunk listing. */
export interface PartitionChunk {
  link: string;
  content: string;
  metadata: Record<string, unknown>;
}

/** One chunk fetched by id (GET /extract/{id}). */
export interface Extract {
  page_content: string;
  metadata: Record<string, unknown>;
}

export function getFileDetail(partition: string, fileId: string, limit?: number): Promise<FileDetail> {
  const qs = limit != null ? `?limit=${limit}` : "";
  return request<FileDetail>(`${P}/${enc(partition)}/file/${enc(fileId)}${qs}`);
}

/** All chunks in a partition, with content. Drops the (large) embedding by default. */
export function listPartitionChunks(partition: string, includeEmbedding = false): Promise<{ chunks: PartitionChunk[] }> {
  return request<{ chunks: PartitionChunk[] }>(`${P}/${enc(partition)}/chunks?include_embedding=${includeEmbedding}`);
}

/** Chunks belonging to one file, with content — filters the partition chunk list by file_id. */
export async function listFileChunks(partition: string, fileId: string): Promise<PartitionChunk[]> {
  const { chunks } = await listPartitionChunks(partition, false);
  return chunks.filter((c) => c.metadata?.file_id === fileId);
}

export function getExtract(extractId: string): Promise<Extract> {
  return request<Extract>(`/extract/${enc(extractId)}`);
}

/** Pull the trailing `{extract_id}` out of a server-built `/extract/{id}` link. */
export function extractIdFromLink(link: string): string {
  return decodeURIComponent(link.replace(/\/+$/, "").split("/").pop() ?? "");
}
