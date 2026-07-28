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

const PLACEHOLDER_REGEX = /\{(\w+)\}/g;

export function extractPlaceholders(content: string): string[] {
  const matches: string[] = [];
  let m: RegExpExecArray | null;
  PLACEHOLDER_REGEX.lastIndex = 0;
  while ((m = PLACEHOLDER_REGEX.exec(content)) !== null) {
    if (!matches.includes(m[1])) matches.push(m[1]);
  }
  return matches;
}

export function validatePlaceholders(content: string, promptType: string) {
  const used = extractPlaceholders(content);
  const known = new Set((PROMPT_TYPE_VARIABLES[promptType] ?? []).map((v) => v.name));
  const unknown = used.filter((v) => !known.has(v));
  const missing = [...known].filter((v) => !used.includes(v));
  return { used, unknown, missing };
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
