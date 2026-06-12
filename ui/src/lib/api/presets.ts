import { request } from "./client";

export interface PresetResponse {
  name: string;
  preset_type: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface PresetListResponse {
  presets: PresetResponse[];
}

export interface CreatePresetRequest {
  name: string;
  preset_type: string;
  config?: Record<string, unknown>;
}

export interface UpdatePresetRequest {
  name?: string;
  config?: Record<string, unknown>;
}

export interface PresetOptionsResponse {
  chunking_strategies: string[];
  retrieval_pipelines: string[];
  rerankers: string[];
}

const BASE = "/api/v1/admin/presets";

export function getPresetOptions() {
  return request<PresetOptionsResponse>(`${BASE}/options`);
}

export function listPresets(presetType?: string) {
  const search = new URLSearchParams();
  if (presetType) search.set("preset_type", presetType);
  const qs = search.toString();
  return request<PresetListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getPreset(presetType: string, name: string) {
  return request<PresetResponse>(`${BASE}/${presetType}/${name}`);
}

export function createPreset(data: CreatePresetRequest) {
  return request<PresetResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updatePreset(
  presetType: string,
  name: string,
  data: UpdatePresetRequest,
) {
  return request<PresetResponse>(`${BASE}/${presetType}/${name}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function deletePreset(presetType: string, name: string) {
  return request<{ deleted: string }>(`${BASE}/${presetType}/${name}`, {
    method: "DELETE",
  });
}
