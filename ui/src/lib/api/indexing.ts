import { request } from "./client";

export interface IndexResponse {
  document_id: string;
  partition: string;
  chunk_count: number;
  status: string;
}

export interface BatchJobAccepted {
  job_id: string;
  status: string;
  total_documents: number;
}

export function indexDocument(file: File, partition: string) {
  const form = new FormData();
  form.append("file", file);
  form.append("partition", partition);
  return request<IndexResponse>("/api/v1/admin/indexing/document", {
    method: "POST",
    body: form,
  });
}

export function indexBatch(files: File[], partition: string) {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("partition", partition);
  return request<BatchJobAccepted>("/api/v1/admin/indexing/batch", {
    method: "POST",
    body: form,
  });
}

export const BATCH_CHUNK_SIZE = 20;

export interface BatchChunkProgress {
  batchIndex: number;
  totalBatches: number;
  jobId: string;
  jobIds: string[];
}

export async function indexBatchChunked(
  files: File[],
  partition: string,
  onBatchComplete?: (progress: BatchChunkProgress) => void,
): Promise<{ jobIds: string[]; totalDocuments: number }> {
  const chunks: File[][] = [];
  for (let i = 0; i < files.length; i += BATCH_CHUNK_SIZE) {
    chunks.push(files.slice(i, i + BATCH_CHUNK_SIZE));
  }

  const jobIds: string[] = [];
  for (let i = 0; i < chunks.length; i++) {
    const result = await indexBatch(chunks[i], partition);
    jobIds.push(result.job_id);
    onBatchComplete?.({
      batchIndex: i,
      totalBatches: chunks.length,
      jobId: result.job_id,
      jobIds: [...jobIds],
    });
  }

  return { jobIds, totalDocuments: files.length };
}
