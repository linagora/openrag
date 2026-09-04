import { request } from "./client";

// OpenRag pipeline-preset registry (Phase 14). Mounted at `/presets` (admin-only).
// Shapes verified against openrag/api/schemas/admin/preset_schemas.py + routers/admin/presets.py:
//   GET    /presets/options              { chunking_strategies, retrieval_types, reranker_providers }
//   GET    /presets/                     list (bare array; optional ?preset_type=)
//   GET    /presets/{type}/{name}        get one
//   POST   /presets/                     create → 201
//   PUT    /presets/{type}/{name}        update
//   DELETE /presets/{type}/{name}        delete → 204

export type PresetType = "indexation" | "retrieval";

export interface PresetResponse {
  name: string;
  preset_type: PresetType;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  // Distinct partition count referencing this preset. Only populated by
  // listPresets(); get/create/update responses default this to 0.
  used_by_partitions: number;
}

export interface CreatePresetRequest {
  name: string;
  preset_type: PresetType;
  config?: Record<string, unknown>;
}

export interface UpdatePresetRequest {
  name?: string;
  config?: Record<string, unknown>;
}

export interface PresetOptionsResponse {
  chunking_strategies: string[];
  // Optional: older backends don't return this (UI falls back to a default list).
  parsing_strategies?: string[];
  retrieval_types: string[];
  reranker_providers: string[];
  // Optional: older backends predate context compression.
  compression_available?: boolean;
  compression_backend?: string;
}

const BASE = "/presets";
const enc = encodeURIComponent;

export function getPresetOptions() {
  return request<PresetOptionsResponse>(`${BASE}/options`);
}

/** List presets (bare array). Optionally filter by preset type. */
export function listPresets(presetType?: PresetType) {
  const qs = presetType ? `?preset_type=${enc(presetType)}` : "";
  return request<PresetResponse[]>(`${BASE}/${qs}`);
}

export function getPreset(presetType: PresetType, name: string) {
  return request<PresetResponse>(`${BASE}/${enc(presetType)}/${enc(name)}`);
}

export function createPreset(data: CreatePresetRequest) {
  return request<PresetResponse>(`${BASE}/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updatePreset(
  presetType: PresetType,
  name: string,
  data: UpdatePresetRequest,
) {
  return request<PresetResponse>(`${BASE}/${enc(presetType)}/${enc(name)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function deletePreset(presetType: PresetType, name: string) {
  return request<void>(`${BASE}/${enc(presetType)}/${enc(name)}`, {
    method: "DELETE",
  });
}
