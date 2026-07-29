import type { PromptType } from "@/lib/api/prompts";

// Shared metadata for the managed prompt types: their human labels, how they
// group by concern, and the `{variables}` each template understands. Consumed by
// the Prompt Library page and by the preset/partition editors that select a
// prompt by name.

export interface PromptTypeEntry {
  value: PromptType;
  label: string;
}

export interface PromptGroup {
  // Concern this group of prompts belongs to.
  name: string;
  // Where a prompt of this concern is *selected* (presets vs partition).
  description: string;
  types: PromptTypeEntry[];
}

export const PROMPT_GROUPS: PromptGroup[] = [
  {
    name: "Final Answer",
    description: "The final-answer prompt — selected per partition",
    types: [{ value: "sys_prompt", label: "Final answer prompt" }],
  },
  {
    name: "Indexation",
    description: "Document-enrichment prompts — selected on the indexation preset",
    types: [
      { value: "chunk_contextualizer", label: "Contextualization" },
      { value: "image_captioning", label: "Image captioning" },
      { value: "topic_tagger", label: "Topic tagging" },
    ],
  },
  {
    name: "Retrieval",
    description: "Query-transformation prompts — selected on the retrieval preset",
    types: [
      { value: "query_contextualizer", label: "Query contextualizer" },
      { value: "hyde", label: "HyDE" },
      { value: "multi_query", label: "Multi-query" },
    ],
  },
];

export const PROMPT_TYPES: PromptTypeEntry[] = PROMPT_GROUPS.flatMap((g) => g.types);

const PROMPT_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  PROMPT_TYPES.map((t) => [t.value, t.label]),
);

export function promptTypeLabel(type: string): string {
  return PROMPT_TYPE_LABELS[type] ?? type;
}

export interface TemplateVariable {
  name: string;
  description: string;
  sample: string;
}

// Placeholders each template understands, keyed by prompt type. Empty arrays are
// intentional: those prompts are system messages sent with the chunk/image
// attached as a separate message — they take no inline placeholders.
export const PROMPT_TYPE_VARIABLES: Record<string, TemplateVariable[]> = {
  sys_prompt: [
    { name: "context", description: "Retrieved document chunks injected by the pipeline", sample: "[Source 1] Employees are entitled to 20 days of paid vacation per year, accrued monthly." },
    { name: "current_date", description: "Today's date, injected at request time", sample: "2026-07-27" },
  ],
  query_contextualizer: [
    { name: "current_date", description: "Today's date, injected at request time", sample: "2026-07-27" },
    { name: "query_language", description: "Detected language of the user's query", sample: "English" },
  ],
  chunk_contextualizer: [],
  image_captioning: [],
  topic_tagger: [],
  hyde: [
    { name: "question", description: "The user's search query", sample: "How do I configure SSL certificates?" },
  ],
  multi_query: [
    { name: "k_queries", description: "Number of alternative queries to generate", sample: "3" },
    { name: "query", description: "The user's original search query", sample: "What are the termination clauses in the contract?" },
  ],
};

export interface TemplateScan {
  /** Root field names, reduced the way the backend reduces them. */
  fields: string[];
  /** True when Python's parser would raise — i.e. the API would return 422. */
  malformed: boolean;
  error?: string;
}

/** Scan a template the way Python's ``string.Formatter().parse`` does.
 *
 *  The backend validates with the real thing, so approximating it with a
 *  `/\{(\w+)\}/` regex let templates through that were rejected only on save:
 *  `{{` / `}}` are literal braces, a lone brace is an error, and a field may
 *  carry a conversion (`!r`), a format spec (`:>10`) or attribute/index access
 *  (`a.b`, `a[0]`) — all of which reduce to the root name.
 */
export function scanTemplate(content: string): TemplateScan {
  const fields: string[] = [];
  let i = 0;
  while (i < content.length) {
    const ch = content[i];
    if (ch === "{") {
      if (content[i + 1] === "{") {
        i += 2;
        continue;
      }
      const end = content.indexOf("}", i + 1);
      if (end === -1) {
        return { fields, malformed: true, error: "Single '{' encountered in format string" };
      }
      const root = content
        .slice(i + 1, end)
        .split("!")[0]
        .split(":")[0]
        .split(".")[0]
        .split("[")[0]
        .trim();
      if (!fields.includes(root)) fields.push(root);
      i = end + 1;
      continue;
    }
    if (ch === "}") {
      if (content[i + 1] === "}") {
        i += 2;
        continue;
      }
      return { fields, malformed: true, error: "Single '}' encountered in format string" };
    }
    i += 1;
  }
  return { fields, malformed: false };
}

export function extractPlaceholders(content: string): string[] {
  return scanTemplate(content).fields;
}

/** Types the backend renders with `str.format` and therefore validates.
 *
 *  Mirrors `_PROMPT_FORMAT_FIELDS` in `prompt_service.py`. It cannot be derived
 *  from `PROMPT_TYPE_VARIABLES`, because a type there may legitimately have an
 *  empty variable list; the others are sent to the model verbatim, so braces in
 *  them are ordinary characters and must not be validated at all.
 */
const FORMATTED_PROMPT_TYPES = new Set([
  "sys_prompt",
  "query_contextualizer",
  "hyde",
  "multi_query",
]);

export function validatePlaceholders(content: string, promptType: string) {
  const known = new Set((PROMPT_TYPE_VARIABLES[promptType] ?? []).map((v) => v.name));
  if (!FORMATTED_PROMPT_TYPES.has(promptType)) {
    return { used: [], unknown: [], missing: [], malformed: false, error: undefined };
  }
  const scan = scanTemplate(content);
  const used = scan.fields;
  const unknown = used.filter((v) => !known.has(v));
  const missing = [...known].filter((v) => !used.includes(v));
  return { used, unknown, missing, malformed: scan.malformed, error: scan.error };
}

export interface PreviewSegment {
  value: string;
  isVariable: boolean;
  varName?: string;
}

/** Substitute each known `{var}` with its sample value, tracking which spans
 *  were substituted so the preview can highlight them. */
export function renderPreview(content: string, promptType: string): PreviewSegment[] {
  const vars = PROMPT_TYPE_VARIABLES[promptType] ?? [];
  const varMap = Object.fromEntries(vars.map((v) => [v.name, v.sample]));

  const segments: PreviewSegment[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  const regex = /\{(\w+)\}/g;

  while ((m = regex.exec(content)) !== null) {
    if (m.index > lastIndex) {
      segments.push({ value: content.slice(lastIndex, m.index), isVariable: false });
    }
    const varName = m[1];
    const sample = varMap[varName];
    if (sample !== undefined) {
      segments.push({ value: sample, isVariable: true, varName });
    } else {
      segments.push({ value: m[0], isVariable: false });
    }
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < content.length) {
    segments.push({ value: content.slice(lastIndex), isVariable: false });
  }
  return segments;
}

/* ---------- Prompt picker option values ---------- */

/** Sentinel for the "use the type's global default" choice.
 *
 *  Prompt names are free text, so any bare sentinel is a name a prompt could
 *  legitimately have — a prompt actually called `__default__` would then be
 *  impossible to select. Real options are therefore namespaced with a prefix
 *  and the sentinel is the only unprefixed value, which makes a collision
 *  structurally impossible rather than merely unlikely.
 */
export const PROMPT_DEFAULT_OPTION = "__use_default__";
const PROMPT_OPTION_PREFIX = "name:";

export function promptOptionValue(name: string): string {
  return `${PROMPT_OPTION_PREFIX}${name}`;
}

export function promptOptionToName(value: string): string {
  return value.startsWith(PROMPT_OPTION_PREFIX) ? value.slice(PROMPT_OPTION_PREFIX.length) : "";
}

/** The Select value for a stored prompt name ("" / unset → the default). */
export function promptSelectValue(name: string | undefined | null): string {
  return name ? promptOptionValue(name) : PROMPT_DEFAULT_OPTION;
}
