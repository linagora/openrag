/**
 * The single source of truth for the release notes shown in the Admin Console.
 * Update this object for each release; the sidebar label, unread state, and
 * dialog content are derived from it.
 */
export interface ReleaseNotes {
  version: string;
  date: string;
  summary: string;
  whatsNew: readonly string[];
}

export const releaseNotes: ReleaseNotes = {
  version: "2.2.0",
  date: "2026-08-31",
  summary: "OpenRAG 2.2 improves deployment compatibility, OpenAI API support, and indexing quality while giving applications finer control over prompts, retrieval, and LLM routing.",
  whatsNew: [
    "Milvus 3.0 is now required. Follow the migration guide before upgrading an existing Milvus 2.x deployment.",
    "Client-provided system instructions are preserved and safely integrated into RAG prompts.",
    "Scope retrieval to selected indexed file IDs in OpenAI chat requests.",
    "Optionally route a request to a custom HTTPS LLM endpoint, model, and credentials after enabling LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT.",
    "OpenAI-compatible tool calls, function calls, message names, and vendor-specific fields are forwarded to the model.",
    "Strict OpenAI-compatible providers no longer receive disabled log-probability parameters.",
    "Meaningful image captions next to placeholders are preserved during indexing.",
  ],
};

export const LAST_VIEWED_RELEASE_NOTES_KEY = "openrag:last-viewed-release-notes-version";

export function hasViewedRelease(version: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(LAST_VIEWED_RELEASE_NOTES_KEY) === version;
  } catch {
    // Local storage can be disabled by the browser. The dialog remains usable;
    // the badge simply stays visible until storage is available again.
    return false;
  }
}

export function markReleaseAsViewed(version: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LAST_VIEWED_RELEASE_NOTES_KEY, version);
  } catch {
    // Treat storage as an enhancement rather than blocking access to notes.
  }
}

export function formatReleaseDate(date: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}
