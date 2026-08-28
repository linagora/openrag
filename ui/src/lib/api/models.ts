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
//   POST   /model-endpoints/{type}/{name}/reveal-api-key
//   POST   /model-endpoints/{type}/{name}/validate → ValidateEndpointResponse (no body)

export type ModelType = "embedder" | "reranker" | "llm" | "vlm" | "stt";

export interface ModelEndpointResponse {
  name: string;
  model_type: ModelType;
  endpoint: string;
  model_name: string | null;
  batch_size: number;
  timeout: number;
  extra: Record<string, unknown>;
  has_api_key?: boolean;
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
  transcription_supported?: boolean | null;
  detail?: string | null;
}

export interface RevealApiKeyResponse {
  api_key: string | null;
}

const BASE = "/model-endpoints";
const enc = encodeURIComponent;
export const REDACTED_SECRET = "<redacted>";
export const API_KEY_DISPLAY_PLACEHOLDER = "sk-********";
export const SECRET_DISPLAY_PLACEHOLDER = "••••••••";
const MASK_SUFFIX = "********";
const MASK_PREFIX_LENGTH = 3;
const LEGACY_API_KEY_DISPLAY_PLACEHOLDER = "********";

const SECRET_FIELD_NAMES = new Set([
  "api_key",
  "api_token",
  "access_key",
  "auth_token",
  "chainlit_auth_secret",
  "client_secret",
  "hf_token",
  "oidc_client_secret",
  "oidc_token_encryption_key",
  "password",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
  "signing_key",
  "token",
  "token_encryption_key",
]);
const SECRET_FIELD_SUFFIXES = [
  "_access_key",
  "_api_key",
  "_auth_token",
  "_password",
  "_private_key",
  "_refresh_token",
  "_secret",
  "_signing_key",
  "_token",
  "_token_encryption_key",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSecretField(key: string | undefined): boolean {
  if (!key) return false;
  const normalized = key.toLowerCase();
  return SECRET_FIELD_NAMES.has(normalized) || SECRET_FIELD_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

function displayPlaceholderFor(key: string | undefined): string {
  return key?.toLowerCase() === "api_key" ? API_KEY_DISPLAY_PLACEHOLDER : SECRET_DISPLAY_PLACEHOLDER;
}

function isPrefixMaskedSecret(value: string): boolean {
  return value.length === MASK_PREFIX_LENGTH + MASK_SUFFIX.length && value.endsWith(MASK_SUFFIX);
}

function isUnchangedPlaceholder(value: string, key: string | undefined): boolean {
  if (value === displayPlaceholderFor(key)) return true;
  if (isPrefixMaskedSecret(value)) return true;
  return key?.toLowerCase() === "api_key" && value === LEGACY_API_KEY_DISPLAY_PLACEHOLDER;
}

function transformSecretPlaceholders(value: unknown, direction: "display" | "submit", key?: string): unknown {
  if (isSecretField(key) && typeof value === "string") {
    if (direction === "display") {
      if (isPrefixMaskedSecret(value)) return value;
      return displayPlaceholderFor(key);
    }
    if (isUnchangedPlaceholder(value, key)) return REDACTED_SECRET;
  }
  if (Array.isArray(value)) {
    return value.map((item) => transformSecretPlaceholders(item, direction));
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, item]) => [
        entryKey,
        transformSecretPlaceholders(item, direction, entryKey),
      ]),
    );
  }
  return value;
}

export function displayModelEndpointExtra(extra: Record<string, unknown>): Record<string, unknown> {
  return transformSecretPlaceholders(extra, "display") as Record<string, unknown>;
}

export function prepareModelEndpointExtraForSubmit(extra: Record<string, unknown>): Record<string, unknown> {
  return transformSecretPlaceholders(extra, "submit") as Record<string, unknown>;
}

export function splitModelEndpointApiKeyExtra(extra: Record<string, unknown>): {
  apiKey: string;
  extra: Record<string, unknown>;
} {
  const displayExtra = displayModelEndpointExtra(extra);
  const { api_key: apiKey, ...rest } = displayExtra;
  return {
    apiKey: typeof apiKey === "string" ? apiKey : "",
    extra: rest,
  };
}

export function mergeModelEndpointApiKeyExtra(
  extra: Record<string, unknown>,
  apiKey: string,
  options: { clearApiKey?: boolean } = {},
): Record<string, unknown> {
  const prepared = prepareModelEndpointExtraForSubmit(extra);
  const normalizedApiKey = prepareModelEndpointExtraForSubmit({ api_key: apiKey.trim() }).api_key;
  if (
    normalizedApiKey === REDACTED_SECRET &&
    typeof prepared.api_key === "string" &&
    prepared.api_key !== REDACTED_SECRET
  ) {
    return prepared;
  }
  if (typeof normalizedApiKey === "string" && normalizedApiKey) {
    return { ...prepared, api_key: normalizedApiKey };
  }
  if (options.clearApiKey) {
    return { ...prepared, api_key: "" };
  }
  return prepared;
}

// LLM token budgets stored in `extra` but surfaced as first-class number fields
// in the endpoint modal (LLM endpoints only). Kept out of the raw "Extra (JSON)"
// box, same as the API key. Backend: core/config/model_endpoints.py.
export const LLM_CONTEXT_SIZE_KEY = "max_llm_context_size";
export const LLM_OUTPUT_TOKENS_KEY = "max_output_tokens";
export const STT_LANGUAGE_KEY = "language";
export const STT_TRANSCRIPT_OUTPUT_FORMAT_KEY = "transcript_output_format";
export const MOSS_TIMESTAMPED_TRANSCRIPT_OUTPUT_FORMAT = "moss_timestamped";
export const RAW_TRANSCRIPT_OUTPUT_FORMAT = "raw";

export interface LlmContextFields {
  maxContextSize: string;
  maxOutputTokens: string;
}

/** Pull the two LLM budget keys out of `extra` into form-field strings. */
export function splitModelEndpointLlmContext(extra: Record<string, unknown>): {
  llmContext: LlmContextFields;
  extra: Record<string, unknown>;
} {
  const { [LLM_CONTEXT_SIZE_KEY]: ctx, [LLM_OUTPUT_TOKENS_KEY]: out, ...rest } = extra;
  const asField = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? String(v) : "");
  return {
    llmContext: { maxContextSize: asField(ctx), maxOutputTokens: asField(out) },
    extra: rest,
  };
}

/** Merge the two LLM budget fields back into `extra`. Blank clears the override
 *  (deletes the key → server falls back to the global default); a non-blank
 *  value is sent as a number for the server to validate as a positive int. */
export function mergeModelEndpointLlmContext(
  extra: Record<string, unknown>,
  fields: LlmContextFields,
): Record<string, unknown> {
  const result = { ...extra };
  const apply = (key: string, raw: string) => {
    const trimmed = raw.trim();
    if (trimmed === "") {
      delete result[key];
      return;
    }
    const n = Number(trimmed);
    if (Number.isFinite(n)) result[key] = n;
    else delete result[key];
  };
  apply(LLM_CONTEXT_SIZE_KEY, fields.maxContextSize);
  apply(LLM_OUTPUT_TOKENS_KEY, fields.maxOutputTokens);
  return result;
}

/** Pull the optional STT language hint out of `extra` into its own form field. */
export function splitModelEndpointSttLanguage(extra: Record<string, unknown>): {
  languageHint: string;
  extra: Record<string, unknown>;
} {
  const { [STT_LANGUAGE_KEY]: language, ...rest } = extra;
  return {
    languageHint: typeof language === "string" ? language : "",
    extra: rest,
  };
}

/** Merge a blank-or-string STT language hint back into the endpoint's `extra`. */
export function mergeModelEndpointSttLanguage(
  extra: Record<string, unknown>,
  languageHint: string,
): Record<string, unknown> {
  const result = { ...extra };
  const normalized = languageHint.trim();
  if (normalized) result[STT_LANGUAGE_KEY] = normalized;
  else delete result[STT_LANGUAGE_KEY];
  return result;
}

/** The MOSS-only UI control is shown for the published model ID and served aliases. */
export function isMossTranscribeDiarizeModel(modelName: string | null | undefined): boolean {
  return modelName?.trim().toLowerCase().includes("moss-transcribe-diarize") ?? false;
}

/** Pull MOSS response formatting out of raw endpoint extra for its dedicated control. */
export function splitModelEndpointMossTranscriptOutput(extra: Record<string, unknown>): {
  mossTimestamped: boolean;
  extra: Record<string, unknown>;
} {
  const { [STT_TRANSCRIPT_OUTPUT_FORMAT_KEY]: outputFormat, ...rest } = extra;
  return {
    mossTimestamped: outputFormat === MOSS_TIMESTAMPED_TRANSCRIPT_OUTPUT_FORMAT,
    extra: rest,
  };
}

/** Store only the enabled MOSS formatter; absence preserves the raw response. */
export function mergeModelEndpointMossTranscriptOutput(
  extra: Record<string, unknown>,
  enabled: boolean,
): Record<string, unknown> {
  const result = { ...extra };
  if (enabled) result[STT_TRANSCRIPT_OUTPUT_FORMAT_KEY] = MOSS_TIMESTAMPED_TRANSCRIPT_OUTPUT_FORMAT;
  else delete result[STT_TRANSCRIPT_OUTPUT_FORMAT_KEY];
  return result;
}

// Which client class an endpoint is built with — a control key inside `extra`
// (see di/factories.py:make_component_factory), surfaced here as a per-type
// "Vendor" dropdown instead of free-text JSON. Options mirror the registries
// in openrag/services/inference/*.py; defaults mirror openrag/di/container.py.
export const IMPLEMENTATION_KEY = "implementation";

export const VENDOR_OPTIONS_BY_TYPE: Record<ModelType, string[]> = {
  embedder: ["vllm", "ollama"],
  reranker: ["infinity", "openai", "tei"],
  llm: ["vllm", "ollama"],
  vlm: ["vllm"],
  // STT uses the OpenAI-compatible audio-transcription API directly; there
  // is no model-client implementation to select.
  stt: [],
};

export const DEFAULT_VENDOR_BY_TYPE: Record<ModelType, string> = {
  embedder: "vllm",
  reranker: "infinity",
  llm: "vllm",
  vlm: "vllm",
  stt: "",
};

/** Pull the vendor/`implementation` control key out of `extra` into a form field. */
export function splitModelEndpointImplementation(extra: Record<string, unknown>): {
  implementation: string;
  extra: Record<string, unknown>;
} {
  const { [IMPLEMENTATION_KEY]: implementation, ...rest } = extra;
  return {
    implementation: typeof implementation === "string" ? implementation : "",
    extra: rest,
  };
}

/** Merge the vendor field back into `extra`. Blank omits the key entirely
 *  (server falls back to its own `default_impl` per model type). */
export function mergeModelEndpointImplementation(
  extra: Record<string, unknown>,
  implementation: string,
): Record<string, unknown> {
  const result = { ...extra };
  if (implementation.trim()) {
    result[IMPLEMENTATION_KEY] = implementation.trim();
  } else {
    delete result[IMPLEMENTATION_KEY];
  }
  return result;
}

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

export function revealModelEndpointApiKey(modelType: ModelType, name: string) {
  return request<RevealApiKeyResponse>(
    `${BASE}/${enc(modelType)}/${enc(name)}/reveal-api-key`,
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
  model_type?: ModelType;
  model_name?: string;
  timeout?: number;
  extra?: Record<string, unknown>;
  api_key?: string;
  stored_api_key_model_type?: ModelType;
  stored_api_key_name?: string;
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

/**
 * Pick the endpoint a picker should pre-select: the one flagged `is_default`,
 * or the only one if exactly one is registered, otherwise none (let the user
 * choose). Lets selection fields show a concrete endpoint instead of sitting
 * empty when the choice is unambiguous.
 */
export function pickDefaultEndpoint(
  endpoints: ModelEndpointResponse[] | undefined | null,
): ModelEndpointResponse | undefined {
  if (!endpoints?.length) return undefined;
  return endpoints.find((e) => e.is_default) ?? (endpoints.length === 1 ? endpoints[0] : undefined);
}

/**
 * Display name for a partition's embedder. The backend stores the sentinel
 * `"default"` (resolved to the `is_default` endpoint at runtime); show the real
 * endpoint name instead — unless that endpoint is itself literally named
 * "default", or the default can't be resolved yet (then fall back to the raw value).
 */
export function resolveEmbedderName(
  value: string | null | undefined,
  embedderEndpoints: ModelEndpointResponse[] | undefined | null,
): string {
  if (value !== "default") return value || "—";
  return pickDefaultEndpoint(embedderEndpoints)?.name ?? "default";
}
