import { request } from "./client";

// OpenRag ops endpoints (admin-only except health_check/version):
//   GET  /health_check           → "RAG API is up." (string)
//   GET  /version                → { version }
//   GET  /config                 → loaded settings (admin)
//   GET  /metrics                → Prometheus text (admin)
//   GET  /actors/                → { actors: [...] } Ray actors (admin)
//   POST /actors/{name}/restart  → { message, actor_name, actor_id } (admin)

export interface VersionResponse {
  version: string;
}

export interface SystemConfig extends Record<string, unknown> {
  chainlit_enabled?: boolean;
  grafana_url?: string | null;
  super_admin_mode?: boolean;
}

export interface RayActor {
  actor_id: string;
  name: string;
  class_name: string;
  state: string;
  namespace: string;
}

export function healthCheck() {
  return request<string>("/health_check");
}

export function getVersion() {
  return request<VersionResponse>("/version");
}

export function getConfig() {
  return request<SystemConfig>("/config");
}

export function getMetrics() {
  return request<string>("/metrics");
}

export function listActors() {
  return request<{ actors: RayActor[] }>("/actors/");
}

export function restartActor(name: string) {
  return request<{ message: string; actor_name: string; actor_id: string }>(
    `/actors/${encodeURIComponent(name)}/restart`,
    { method: "POST" },
  );
}
