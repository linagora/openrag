import { request } from "./client";

// Admin evaluation endpoints:
//   GET    /evaluation/datasets          → EvalDataset[]
//   POST   /evaluation/datasets          → EvalDataset (multipart)
//   DELETE /evaluation/datasets/{id}     → 204
//   GET    /evaluation/runs              → EvalRunSummary[]
//   POST   /evaluation/runs              → EvalRun (202, 409 when one is in flight)
//   GET    /evaluation/runs/{id}         → EvalRun
//   POST   /evaluation/runs/{id}/cancel  → EvalRun

export interface EvalDataset {
  id: string;
  name: string;
  corpus_file_count: number;
  testset_row_count: number;
  created_at: string | null;
  created_by: number | null;
}

export type EvalRunStatus =
  | "QUEUED"
  | "INDEXING"
  | "EVALUATING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export const ACTIVE_RUN_STATUSES: EvalRunStatus[] = ["QUEUED", "INDEXING", "EVALUATING"];

export function isActiveStatus(status: EvalRunStatus): boolean {
  return ACTIVE_RUN_STATUSES.includes(status);
}

/** Refetch cadence while a run can still change, shared by both eval views. */
export const EVAL_POLL_MS = 3000;

export interface FileIndexingSample {
  filename: string;
  size_bytes: number;
  duration_seconds: number;
  failed: boolean;
}

export interface IndexingMetrics {
  files_total: number;
  files_failed: number;
  bytes_total: number;
  wall_seconds: number;
  files_per_minute: number;
  megabytes_per_second: number;
  p50_seconds: number;
  p95_seconds: number;
  by_extension: Record<string, Record<string, number>>;
  samples: FileIndexingSample[];
}

export interface RetrievalMetrics {
  scored_cases: number;
  skipped_cases: number;
  hit_rate: number;
  mrr: number;
  recall: number;
  context_relevance: number | null;
}

export interface AnswerMetrics {
  scored_cases: number;
  pass_rate: number;
  factuality: number | null;
  rubric_score: number | null;
}

export interface EvalCase {
  query: string;
  retrieved_file_ids: string[];
  expected_file_ids: string[];
  hit: boolean | null;
  reciprocal_rank: number | null;
  answer: string | null;
  answer_passed: boolean | null;
  grader_reason: string | null;
}

export interface EvalRun {
  id: string;
  dataset_id: string;
  status: EvalRunStatus;
  started_at: string | null;
  finished_at: string | null;
  indexing: IndexingMetrics | null;
  retrieval: RetrievalMetrics | null;
  answer: AnswerMetrics | null;
  cases: EvalCase[];
  error: string | null;
  created_by: number | null;
}

export interface EvalRunSummary {
  id: string;
  dataset_id: string;
  status: EvalRunStatus;
  started_at: string | null;
  finished_at: string | null;
  hit_rate: number | null;
  mrr: number | null;
  answer_pass_rate: number | null;
  files_per_minute: number | null;
  error: string | null;
}

export function listEvalDatasets() {
  return request<EvalDataset[]>("/evaluation/datasets");
}

export function createEvalDataset(name: string, testset: File, corpus: File[]) {
  const body = new FormData();
  body.append("name", name);
  body.append("testset", testset);
  // FastAPI reads repeated parts as `list[UploadFile]`.
  corpus.forEach((file) => body.append("corpus", file));
  return request<EvalDataset>("/evaluation/datasets", { method: "POST", body });
}

export function deleteEvalDataset(id: string) {
  return request<void>(`/evaluation/datasets/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function listEvalRuns(limit = 50) {
  return request<EvalRunSummary[]>(`/evaluation/runs?limit=${limit}`);
}

export function startEvalRun(datasetId: string) {
  return request<EvalRun>("/evaluation/runs", {
    method: "POST",
    body: JSON.stringify({ dataset_id: datasetId }),
  });
}

export function getEvalRun(id: string) {
  return request<EvalRun>(`/evaluation/runs/${encodeURIComponent(id)}`);
}

export function cancelEvalRun(id: string) {
  return request<EvalRun>(`/evaluation/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}
