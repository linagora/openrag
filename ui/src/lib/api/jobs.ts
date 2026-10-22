import { request } from "./client";

// OpenRag jobs/queue API (poll-based — OpenRag has NO task SSE).
// Verified vs routers/admin/jobs.py + routers/admin/indexing.py. The jobs
// pages poll getTaskStatus on a refetchInterval, stopping
// once isTerminalState(state).
//   GET    /queue/info                          queue + worker pool summary
//   GET    /queue/tasks[?task_status=]          list tasks → { tasks: [...] }
//   GET    /indexer/task/{id}                   status → { task_id, task_state, details, error_url? }
//   GET    /indexer/task/{id}/stages            bounded stage timeline and quality
//   GET    /indexer/task/{id}/error             { task_id, reason?, summary?, traceback: string[] }
//   DELETE /indexer/task/{id}                   cancel → { message }

const QUEUE = "/queue";
const TASK = "/indexer/task";

export type TaskState =
  | "QUEUED"
  | "SERIALIZING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

const ACTIVE_STATES: readonly TaskState[] = ["QUEUED", "SERIALIZING"];
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
  degraded_stages?: string[];
}

export type TaskOutcome = "active" | "completed" | "completed_degraded" | "failed" | "cancelled";

export type StageHistoryQuality =
  | "complete"
  | "pending_persistence"
  | "incomplete"
  | "unknown"
  | "truncated"
  | "legacy"
  | "not_started";

export interface StageProgressSummary {
  attempt?: number;
  phase?: string;
  stage_id?: string | null;
  stage_label?: string | null;
  stage_rank?: number | null;
  position_score?: number;
  is_currently_running: boolean;
  elapsed_ms?: number | null;
  elapsed_sampled_at?: string | null;
  freshness: "live" | "durable" | "unknown";
  history_quality: StageHistoryQuality;
  latest_sequence?: number;
  persisted_through_sequence: number;
  expected_final_sequence?: number | null;
  current_invocation_id?: string | null;
  interrupted_invocation_id?: string | null;
  last_known_stage_id?: string | null;
  last_known_stage_label?: string | null;
  history_truncated?: boolean;
  known_loss?: boolean;
}

export interface StageProgressEvent {
  event_id: string;
  task_id: string;
  attempt: number;
  sequence: number;
  invocation_id: string;
  stage_id: string;
  stage_rank?: number | null;
  status: "started" | "completed" | "failed" | "cancelled";
  occurred_at: string;
  duration_ms?: number | null;
  failure_effect?: "continued_degraded" | "task_failed" | null;
}

export interface StageProgressPage extends StageProgressSummary {
  events: StageProgressEvent[];
  has_more: boolean;
  next_cursor: { attempt: number; sequence: number } | null;
}

/** Row from GET /queue/tasks — note `state` (vs `task_state` in the detail). */
export interface TaskListItem {
  task_id: string;
  state: TaskState;
  outcome: TaskOutcome;
  details: TaskDetails;
  created_at?: string | null;
  duration_ms?: number | null;
  stage_progress?: StageProgressSummary;
  error_summary?: string;
  url: string;
  error_url?: string;
}

export interface TaskListPagination {
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
  available_partitions: string[];
}

export interface TaskListResponse {
  tasks: TaskListItem[];
  pagination?: TaskListPagination;
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

export function listTasks(
  taskStatus?: string,
  pagination?: { limit: number; offset: number },
): Promise<TaskListResponse> {
  const query = new URLSearchParams();
  if (taskStatus) query.set("task_status", taskStatus);
  if (pagination) {
    query.set("limit", String(pagination.limit));
    query.set("offset", String(pagination.offset));
  }
  const qs = query.size ? `?${query.toString()}` : "";
  return request<TaskListResponse>(`${QUEUE}/tasks${qs}`);
}

export function getTaskStatus(taskId: string): Promise<TaskStatus> {
  return request<TaskStatus>(`${TASK}/${encodeURIComponent(taskId)}`);
}

export function getTaskStageProgress(
  taskId: string,
  cursor?: { attempt: number; sequence: number } | null,
  limit = 50,
): Promise<StageProgressPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) {
    query.set("before_attempt", String(cursor.attempt));
    query.set("before_sequence", String(cursor.sequence));
  }
  return request<StageProgressPage>(`${TASK}/${encodeURIComponent(taskId)}/stages?${query.toString()}`);
}

export function getTaskError(taskId: string): Promise<{
  task_id: string;
  reason?: string;
  summary?: string;
  traceback: string[];
}> {
  return request<{ task_id: string; reason?: string; summary?: string; traceback: string[] }>(
    `${TASK}/${encodeURIComponent(taskId)}/error`,
  );
}

export function cancelTask(taskId: string): Promise<{ message: string }> {
  return request<{ message: string }>(`${TASK}/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}
