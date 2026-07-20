import { request } from "./client";

// OpenRag jobs/queue API (poll-based — OpenRag has NO task SSE).
// Verified vs routers/admin/jobs.py + routers/admin/indexing.py. The jobs
// pages poll getTaskStatus (+ getTaskLogs) on a refetchInterval, stopping
// once isTerminalState(state).
//   GET    /queue/info                          queue + worker pool summary
//   GET    /queue/tasks[?task_status=]          list tasks → { tasks: [...] }
//   GET    /indexer/task/{id}                   status → { task_id, task_state, details, error_url? }
//   GET    /indexer/task/{id}/error             { task_id, traceback: string[] }
//   GET    /indexer/task/{id}/logs[?max_lines]  { task_id, logs: string[] }
//   DELETE /indexer/task/{id}                   cancel → { message }

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
  failed_stage?: string;
}

/** Row from GET /queue/tasks — note `state` (vs `task_state` in the detail). */
export interface TaskListItem {
  task_id: string;
  state: TaskState;
  details: TaskDetails;
  created_at?: string | null;
  duration_ms?: number | null;
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
