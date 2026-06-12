import { request } from "./client";

export interface RayNode {
  id: string;
  alive: boolean;
  address: string;
  is_head: boolean;
  resources: Record<string, number>;
}

export interface MarkerWorker {
  name: string;
  status: string;
  ping: string;
}

export interface MarkerPoolSummary {
  max_actors: number;
  spawned: number;
  alive: number;
}

export interface ServeDeployment {
  name: string;
  status: string;
  message: string;
}

export interface RayDetail {
  nodes: RayNode[];
  serve_deployments: ServeDeployment[];
  marker_pool: MarkerWorker[];
  marker_pool_summary: MarkerPoolSummary;
}

export interface HealthResponse {
  status: string;
  services: Record<string, boolean>;
  ray_detail: RayDetail | null;
}

export function health() {
  return request<HealthResponse>("/api/v1/admin/system/health");
}

export function config() {
  return request<Record<string, unknown>>("/api/v1/admin/system/config");
}

export function metrics() {
  return request<string>("/api/v1/admin/system/metrics");
}

export function restartMarkerWorker(index: number) {
  return request<MarkerWorker>(
    `/api/v1/admin/system/marker-worker/${index}/restart`,
    { method: "POST" },
  );
}

export function restartMarkerPool() {
  return request<{ workers: MarkerWorker[] }>(
    "/api/v1/admin/system/marker-pool/restart",
    { method: "POST" },
  );
}
