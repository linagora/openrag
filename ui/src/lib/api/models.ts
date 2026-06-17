import { request } from "./client";

// OpenRag model-endpoint registry (Phase 14). Mounted at `/model-endpoints`
// (admin-only). Shapes verified against
// openrag/api/schemas/admin/model_endpoint_schemas.py + routers/admin/model_endpoints.py:
//   GET    /model-endpoints/                       list (bare array; optional ?model_type=)
//   GET    /model-endpoints/{type}/{name}          get one
//   POST   /model-endpoints/                       create → 201
//   PUT    /model-endpoints/{type}/{name}          update
//   DELETE /model-endpoints/{type}/{name}          delete → 204
//   POST   /model-endpoints/{type}/{name}/set-default
//   POST   /model-endpoints/{type}/{name}/validate → ValidateEndpointResponse (no body)

export type ModelType = "embedder" | "reranker" | "llm" | "vlm";

export interface ModelEndpointResponse {
  name: string;
  model_type: ModelType;
  endpoint: string;
  model_name: string | null;
  batch_size: number;
  timeout: number;
  extra: Record<string, unknown>;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateModelEndpointRequest {
  name: string;
  model_type: ModelType;
  endpoint: string;
  model_name?: string | null;
  batch_size?: number;
  timeout?: number;
  extra?: Record<string, unknown>;
  is_default?: boolean;
}

/** PUT body — every field optional; `name` renames the endpoint. */
export interface UpdateModelEndpointRequest {
  name?: string;
  endpoint?: string;
  model_name?: string | null;
  batch_size?: number;
  timeout?: number;
  extra?: Record<string, unknown>;
  is_default?: boolean;
}

export interface ValidateModelEndpointResponse {
  reachable: boolean;
  model_found?: boolean | null;
  models_served?: string[] | null;
  detail?: string | null;
}

const BASE = "/model-endpoints";
const enc = encodeURIComponent;

/** List endpoints (bare array). Optionally filter by model type. */
export function listModelEndpoints(modelType?: ModelType) {
  const qs = modelType ? `?model_type=${enc(modelType)}` : "";
  return request<ModelEndpointResponse[]>(`${BASE}/${qs}`);
}

export function getModelEndpoint(modelType: ModelType, name: string) {
  return request<ModelEndpointResponse>(`${BASE}/${enc(modelType)}/${enc(name)}`);
}

export function createModelEndpoint(data: CreateModelEndpointRequest) {
  return request<ModelEndpointResponse>(`${BASE}/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateModelEndpoint(
  modelType: ModelType,
  name: string,
  data: UpdateModelEndpointRequest,
) {
  return request<ModelEndpointResponse>(`${BASE}/${enc(modelType)}/${enc(name)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function setDefaultModelEndpoint(modelType: ModelType, name: string) {
  return request<ModelEndpointResponse>(
    `${BASE}/${enc(modelType)}/${enc(name)}/set-default`,
    { method: "POST" },
  );
}

export function deleteModelEndpoint(modelType: ModelType, name: string) {
  return request<void>(`${BASE}/${enc(modelType)}/${enc(name)}`, {
    method: "DELETE",
  });
}

export interface ValidateModelEndpointRequest {
  endpoint: string;
  model_name?: string;
  api_key?: string;
}

/**
 * Probe endpoint values (reachability + whether the model is served) BEFORE
 * saving — validates exactly what's in the form, so typos / dead endpoints are
 * caught pre-save.
 */
export function validateModelEndpoint(data: ValidateModelEndpointRequest) {
  return request<ValidateModelEndpointResponse>(`${BASE}/validate`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** Validate an already-saved endpoint by (type, name) — used where only the
 *  stored endpoint reference is available (e.g. a partition's chat LLM). */
export function validateStoredModelEndpoint(modelType: ModelType, name: string) {
  return request<ValidateModelEndpointResponse>(
    `${BASE}/${enc(modelType)}/${enc(name)}/validate`,
    { method: "POST" },
  );
}
