import { isSecretField, type ModelEndpointResponse, type UpdateModelEndpointRequest } from "@/lib/api/models";

/** One field an endpoint edit would change, as the confirmation renders it. */
export type EndpointFieldChange = {
  field: string;
  label: string;
  from: string;
  to: string;
  /** True when this change can land queries in a different vector space than
   *  the one the partition's files were built in. */
  material: boolean;
};

/** The fields that decide which model actually serves an embedding request.
 *
 *  `name` is not one of them: it is a label, and #770 cascades a rename to every
 *  stored reference. `batch_size` and `timeout` are throughput knobs — they
 *  change how the vectors are fetched, never what they are.
 */
const MATERIAL_FIELDS = new Set(["endpoint", "model_name"]);

/** `extra` keys that change the vectors themselves.
 *
 *  `implementation` picks the client, and so the request an embedding is made
 *  with. `max_model_len` becomes the embedder's `truncate_prompt_tokens`, so
 *  lowering it silently shortens every chunk past the new limit — the same
 *  model, the same endpoint, different vectors. The rest of `extra`
 *  (`embed_concurrency`, `batch_size`) is throughput, like the columns above.
 */
const MATERIAL_EXTRA_KEYS = new Set(["implementation", "max_model_len"]);

const LABELS: Record<string, string> = {
  name: "Name",
  endpoint: "URL",
  model_name: "Model",
  batch_size: "Batch size",
  timeout: "Timeout",
};

/** Render a field value for display, distinguishing "unset" from "empty". */
function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "not set";
  return String(value);
}

/** Fields this edit would change, in a fixed order, most consequential first.
 *
 *  Secret `extra` keys are skipped: a stored API key comes back redacted, so
 *  comparing it against the form's value would report a change on every save.
 *  Everything else in `extra` is diffed — some of it decides the vectors.
 */
export function diffEndpointUpdate(
  editing: ModelEndpointResponse,
  update: UpdateModelEndpointRequest,
  /** Values the form filled in for keys the stored endpoint left unset — its
   *  own defaults, which it stamps into `extra` on save whether or not anyone
   *  touched them. Without them an untouched form reports "not set → vllm". */
  formDefaults: Record<string, unknown> = {},
): EndpointFieldChange[] {
  const before: Record<string, unknown> = {
    name: editing.name,
    endpoint: editing.endpoint,
    model_name: editing.model_name,
    batch_size: editing.batch_size,
    timeout: editing.timeout,
  };
  // Only an embedder owns a vector space. Repointing a reranker or an LLM
  // changes future answers, never the stored vectors, so nothing is stranded.
  const isEmbedder = editing.model_type === "embedder";

  const changes: EndpointFieldChange[] = [];
  for (const field of ["endpoint", "model_name", "name", "batch_size", "timeout"]) {
    if (!(field in update)) continue;
    const to = update[field as keyof UpdateModelEndpointRequest];
    // `model_name` is sent as undefined when cleared, which `in` still reports.
    const from = before[field];
    if (show(from) === show(to)) continue;
    changes.push({
      field,
      label: LABELS[field] ?? field,
      from: show(from),
      to: show(to),
      material: isEmbedder && MATERIAL_FIELDS.has(field),
    });
  }
  changes.push(...diffExtra({ ...formDefaults, ...(editing.extra ?? {}) }, update.extra, isEmbedder));
  return changes;
}

/** Per-key changes in `extra`, secrets excluded.
 *
 *  One entry per key rather than one for the whole object: the reader has to
 *  see *which* option moved to judge it, and only some of them are material.
 */
function diffExtra(
  before: Record<string, unknown> | undefined,
  after: Record<string, unknown> | undefined,
  isEmbedder: boolean,
): EndpointFieldChange[] {
  if (after === undefined) return [];
  const from = before ?? {};
  const keys = [...new Set([...Object.keys(from), ...Object.keys(after)])].filter((k) => !isSecretField(k)).sort();
  const changes: EndpointFieldChange[] = [];
  for (const key of keys) {
    if (show(from[key]) === show(after[key])) continue;
    changes.push({
      field: `extra.${key}`,
      label: key,
      from: show(from[key]),
      to: show(after[key]),
      material: isEmbedder && MATERIAL_EXTRA_KEYS.has(key),
    });
  }
  return changes;
}

/** Whether any change here can move the vector space.
 *
 *  Gates the acknowledgement checkbox. A confirmation that demands one for a
 *  timeout tweak teaches people to tick it without reading, which is exactly
 *  the habit that makes it useless on the edit that matters.
 */
export function hasMaterialChange(changes: EndpointFieldChange[]): boolean {
  return changes.some((c) => c.material);
}

/** A free name for a copy of `name`, avoiding the ones already taken.
 *
 *  Offering "create a new one" is only a real alternative if the new one is
 *  one click away, so the create form opens pre-filled under a name that does
 *  not collide.
 */
export function suggestCopyName(name: string, taken: readonly string[]): string {
  const used = new Set(taken);
  const base = name.replace(/-v\d+$/, "");
  for (let n = 2; n < 100; n++) {
    const candidate = `${base}-v${n}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base}-copy`;
}
