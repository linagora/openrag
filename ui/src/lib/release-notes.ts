/**
 * The single source of truth for the release notes shown in the Admin Console.
 * Update this object for each release; the sidebar label, unread state, and
 * dialog content are derived from it.
 */
export type ReleaseNoteSectionId = "highlights" | "openai-api" | "indexing" | "improvements";

export interface ReleaseNoteSection {
  id: ReleaseNoteSectionId;
  title: string;
  items: readonly string[];
}

export interface ReleaseBreakingChanges {
  title: string;
  calloutTitle: string;
  description: string;
  action: string;
}

export interface ReleaseNotes {
  version: string;
  date: string;
  summary: string;
  breakingChanges: ReleaseBreakingChanges;
  sections: readonly ReleaseNoteSection[];
}

export const releaseNotes: ReleaseNotes = {
  version: "2.2.0",
  date: "2026-08-31",
  summary:
    "OpenRAG 2.2 strengthens OpenAI API compatibility, makes retrieval more precise, and improves indexing quality while giving applications more control over prompts and LLM routing.",
  breakingChanges: {
    title: "Breaking Changes",
    calloutTitle: "Milvus 3.0 migration required",
    description: "OpenRAG 2.2 requires Milvus 3.0. Existing Milvus 2.x deployments must be migrated before upgrading.",
    action: "Back up your data and follow the Milvus migration guide before starting the new version.",
  },
  sections: [
    {
      id: "highlights",
      title: "Highlights",
      items: [
        "Limit retrieval to selected indexed file IDs in OpenAI chat requests.",
        "Safely include client-provided system instructions in RAG prompts.",
      ],
    },
    {
      id: "openai-api",
      title: "OpenAI API",
      items: [
        "Forward OpenAI-compatible tool calls, function calls, message names, and vendor-specific fields to the model.",
        "Optionally route a request to a custom HTTPS LLM endpoint, model, and credentials after enabling LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT.",
      ],
    },
    {
      id: "indexing",
      title: "Indexing",
      items: ["Preserve meaningful image captions next to placeholders for more complete indexed content."],
    },
    {
      id: "improvements",
      title: "Improvements",
      items: ["Keep custom HTTPS LLM overrides isolated from the shared LLM circuit breaker."],
    },
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
