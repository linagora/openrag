import { request } from "./client";

export interface ModelEndpointResponse {
  name: string;
  model_type: string;
  endpoint: string;
  model_name: string | null;
  batch_size: number;
  timeout: number;
  extra: Record<string, unknown>;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelEndpointListResponse {
  endpoints: ModelEndpointResponse[];
}

export interface CreateModelEndpointRequest {
  name: string;
  model_type: string;
  endpoint: string;
  model_name?: string;
  batch_size?: number;
  timeout?: number;
  extra?: Record<string, unknown>;
}

export interface UpdateModelEndpointRequest {
  name?: string;
  endpoint?: string;
  model_name?: string;
  batch_size?: number;
  timeout?: number;
  extra?: Record<string, unknown>;
}

export interface ValidateModelEndpointRequest {
  endpoint: string;
  model_name?: string;
  timeout?: number;
  extra?: Record<string, unknown>;
}

export interface ValidateModelEndpointResponse {
  reachable: boolean;
  detail: string | null;
}

const BASE = "/api/v1/admin/model-endpoints";

export function listModelEndpoints(modelType?: string) {
  const search = new URLSearchParams();
  if (modelType) search.set("model_type", modelType);
  const qs = search.toString();
  return request<ModelEndpointListResponse>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getModelEndpoint(modelType: string, name: string) {
  return request<ModelEndpointResponse>(`${BASE}/${modelType}/${name}`);
}

export function createModelEndpoint(data: CreateModelEndpointRequest) {
  return request<ModelEndpointResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateModelEndpoint(
  modelType: string,
  name: string,
  data: UpdateModelEndpointRequest,
) {
  return request<ModelEndpointResponse>(`${BASE}/${modelType}/${name}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function setDefaultModelEndpoint(modelType: string, name: string) {
  return request<ModelEndpointResponse>(
    `${BASE}/${modelType}/set-default/${name}`,
    { method: "POST" },
  );
}

export function deleteModelEndpoint(modelType: string, name: string) {
  return request<{ deleted: string }>(`${BASE}/${modelType}/${name}`, {
    method: "DELETE",
  });
}

export function validateModelEndpoint(data: ValidateModelEndpointRequest) {
  return request<ValidateModelEndpointResponse>(`${BASE}/validate`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}
