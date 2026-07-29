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

type TemplateToken =
  | { kind: "literal"; text: string }
  | { kind: "field"; root: string; raw: string };

interface TokenizeResult {
  tokens: TemplateToken[];
  malformed: boolean;
  error?: string;
}

/** Tokenize a template the way Python's ``string.Formatter().parse`` does.
 *
 *  Single source of grammar for both validation and the preview, so the two can
 *  never disagree about what a template means: `{{`/`}}` are literal braces, a
 *  lone brace is an error, and a field may carry a conversion (`!r`), a format
 *  spec (`:>10`) or attribute/index access (`a.b`, `a[0]`), all of which reduce
 *  to the root name the backend validates against.
 */
function tokenizeTemplate(content: string): TokenizeResult {
  const tokens: TemplateToken[] = [];
  let literal = "";
  let i = 0;
  const flush = () => {
    if (literal) {
      tokens.push({ kind: "literal", text: literal });
      literal = "";
    }
  };
  while (i < content.length) {
    const ch = content[i];
    if (ch === "{") {
      if (content[i + 1] === "{") {
        literal += "{";
        i += 2;
        continue;
      }
      const end = content.indexOf("}", i + 1);
      if (end === -1) {
        flush();
        return { tokens, malformed: true, error: "Single '{' encountered in format string" };
      }
      const raw = content.slice(i + 1, end);
      const root = raw.split("!")[0].split(":")[0].split(".")[0].split("[")[0].trim();
      flush();
      tokens.push({ kind: "field", root, raw });
      i = end + 1;
      continue;
    }
    if (ch === "}") {
      if (content[i + 1] === "}") {
        literal += "}";
        i += 2;
        continue;
      }
      flush();
      return { tokens, malformed: true, error: "Single '}' encountered in format string" };
    }
    literal += ch;
    i += 1;
  }
  flush();
  return { tokens, malformed: false };
}

export function scanTemplate(content: string): TemplateScan {
  const { tokens, malformed, error } = tokenizeTemplate(content);
  const fields: string[] = [];
  for (const token of tokens) {
    if (token.kind === "field" && !fields.includes(token.root)) fields.push(token.root);
  }
  return { fields, malformed, error };
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

/** Render the template the way the pipeline will: substitute each known
 *  `{var}` with its sample value, unescape `{{`/`}}`, and track which spans were
 *  substituted so the preview can highlight them.
 *
 *  Shares `tokenizeTemplate` with validation so what an author sees here matches
 *  what the model receives — a separate regex used to leave escaped braces
 *  doubled and skip formatted fields entirely.
 *
 *  A field's conversion (`!r`) and format spec (`:>12`) are recognised but
 *  deliberately NOT emulated: the substituted value is the illustrative sample,
 *  not the runtime one, so reimplementing Python's format mini-language in
 *  TypeScript would add a second thing to keep in sync for no gain. The preview
 *  shows where a value lands, not its exact padding or quoting.
 */
export function renderPreview(content: string, promptType: string): PreviewSegment[] {
  // Verbatim types are never `.format`-ed at runtime, so they preview as-is.
  if (!FORMATTED_PROMPT_TYPES.has(promptType)) {
    return content ? [{ value: content, isVariable: false }] : [];
  }
  const vars = PROMPT_TYPE_VARIABLES[promptType] ?? [];
  const varMap = Object.fromEntries(vars.map((v) => [v.name, v.sample]));
  const { tokens } = tokenizeTemplate(content);

  return tokens.map((token) => {
    if (token.kind === "literal") return { value: token.text, isVariable: false };
    const sample = varMap[token.root];
    return sample !== undefined
      ? { value: sample, isVariable: true, varName: token.root }
      : { value: `{${token.raw}}`, isVariable: false };
  });
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
