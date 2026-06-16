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

// ── OpenRag jobs/queue (real API) ───────────────────────────────────────────
// Verified vs routers/admin/jobs.py + routers/admin/indexing.py. Mandragora
// streamed per-stage timings over SSE; OpenRag has NO task SSE, so the jobs
// pages degrade to POLLING getTaskStatus (+ getTaskLogs) on a refetchInterval,
// stopping once isTerminalState(state).
//   GET    /queue/info                          queue + worker pool summary
//   GET    /queue/tasks[?task_status=]          list tasks → { tasks: [...] }
//   GET    /indexer/task/{id}                   status → { task_id, task_state, details, error_url? }
//   GET    /indexer/task/{id}/error             { task_id, traceback: string[] }
//   GET    /indexer/task/{id}/logs[?max_lines]  { task_id, logs: string[] }
//   DELETE /indexer/task/{id}                   cancel → { message }
// The pages still use the legacy SSE/flat functions above; they migrate to
// these in the jobs-pages commit.

const QUEUE = "/queue";
const TASK = "/indexer/task";

export type TaskState =
  | "QUEUED"
  | "SERIALIZING"
  | "CHUNKING"
  | "INSERTING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

const ACTIVE_STATES: readonly TaskState[] = ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"];
const TERMINAL_STATES: readonly TaskState[] = ["COMPLETED", "FAILED", "CANCELLED"];

export const isActiveState = (s: string): boolean => (ACTIVE_STATES as readonly string[]).includes(s);
/** True once polling can stop — the task won't change state again. */
export const isTerminalState = (s: string): boolean => (TERMINAL_STATES as readonly string[]).includes(s);

export interface TaskDetails {
  file_id: string;
  partition: string;
  metadata: Record<string, unknown>;
  user_id: number;
}

/** Row from GET /queue/tasks — note `state` (vs `task_state` in the detail). */
export interface TaskListItem {
  task_id: string;
  state: TaskState;
  details: TaskDetails;
  url: string;
  error_url?: string;
}

/** GET /indexer/task/{id} — note `task_state` (vs `state` in list rows). */
export interface TaskStatus {
  task_id: string;
  task_state: TaskState;
  details: TaskDetails;
  error_url?: string;
}

export interface QueueInfo {
  workers: { total_slots: number; pool_size: number; max_per_actor: number };
  tasks: {
    active: number;
    active_statuses: Record<string, number>;
    total_completed: number;
    total_cancelled: number;
    total_failed: number;
  };
}

export function getQueueInfo(): Promise<QueueInfo> {
  return request<QueueInfo>(`${QUEUE}/info`);
}

export function listTasks(taskStatus?: string): Promise<{ tasks: TaskListItem[] }> {
  const qs = taskStatus ? `?task_status=${encodeURIComponent(taskStatus)}` : "";
  return request<{ tasks: TaskListItem[] }>(`${QUEUE}/tasks${qs}`);
}

export function getTaskStatus(taskId: string): Promise<TaskStatus> {
  return request<TaskStatus>(`${TASK}/${encodeURIComponent(taskId)}`);
}

export function getTaskError(taskId: string): Promise<{ task_id: string; traceback: string[] }> {
  return request<{ task_id: string; traceback: string[] }>(`${TASK}/${encodeURIComponent(taskId)}/error`);
}

export function getTaskLogs(taskId: string, maxLines = 100): Promise<{ task_id: string; logs: string[] }> {
  return request<{ task_id: string; logs: string[] }>(`${TASK}/${encodeURIComponent(taskId)}/logs?max_lines=${maxLines}`);
}

export function cancelTask(taskId: string): Promise<{ message: string }> {
  return request<{ message: string }>(`${TASK}/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}
