import { request } from "./client";

export interface StageTiming {
  parse_s?: number;
  caption_s?: number;
  chunk_s?: number;
  contextualize_s?: number;
  embed_s?: number;
  store_s?: number;
}

export interface JobDocumentStatus {
  id: string;
  filename: string;
  status: string;
  chunk_count: number;
  error_message: string | null;
  stage_timings: StageTiming | null;
}

export interface JobResponse {
  id: string;
  status: string;
  total_documents: number;
  partition: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobDetailResponse extends JobResponse {
  documents: JobDocumentStatus[];
}

export interface JobListResponse {
  jobs: JobResponse[];
  offset: number;
  limit: number;
}

export interface ChunkStoredEvent {
  type: "chunk_stored";
  doc_id: string;
  filename: string;
  chunk_index: number;
  stored: boolean;
  timing: StageTiming;
}

export interface JobCompletedEvent {
  type: "job_completed";
  job_id: string;
  status: string;
}

export type JobEvent = ChunkStoredEvent | JobCompletedEvent;

const BASE = "/api/v1/admin/jobs";

export function listJobs(params?: {
  status?: string;
  offset?: number;
  limit?: number;
}) {
  const search = new URLSearchParams();
  if (params?.status) search.set("status", params.status);
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<JobListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getJob(id: string) {
  return request<JobDetailResponse>(`${BASE}/${id}`);
}

export function streamJobEvents(
  jobId: string,
  onChunkStored: (event: ChunkStoredEvent) => void,
  onJobCompleted: (event: JobCompletedEvent) => void,
  onError: (error: Error) => void,
): AbortController {
  const controller = new AbortController();
  const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
  const token = localStorage.getItem("access_token");

  const headers: Record<string, string> = {};
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  fetch(`${apiBase}${BASE}/${jobId}/events`, {
    headers,
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`SSE connection failed: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("No response body");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const event: JobEvent = JSON.parse(line.slice(6));
              if (event.type === "chunk_stored") {
                onChunkStored(event);
              } else if (event.type === "job_completed") {
                onJobCompleted(event);
              }
            } catch {
              // skip malformed lines
            }
          }
        }
      }
    })
    .catch((err) => {
      if (err.name !== "AbortError") {
        onError(err);
      }
    });

  return controller;
}
