/**
 * The single source of truth for the release notes shown in the Admin Console.
 * Update this object for each release; the sidebar label, unread state, and
 * dialog content are derived from it.
 */
export interface ReleaseNotes {
  version: string;
  date: string;
  summary: string;
  features: readonly string[];
  improvements: readonly string[];
  fixes: readonly string[];
  breakingChanges: readonly string[];
}

export const releaseNotes: ReleaseNotes = {
  version: "2.2.0",
  date: "2026-08-31",
  summary: "OpenRAG 2.2 gives administrators more control over external audio transcription and indexing configuration.",
  features: [
    "Manage transcription prompts directly from the Admin Console.",
    "Configure OpenAI-compatible speech-to-text endpoints, including MOSS.",
    "Choose the STT endpoint and transcription prompt for each indexation preset.",
    "Select raw, timestamped, or speaker-aware output for MOSS transcripts.",
  ],
  improvements: [
    "Show clearer validation feedback while configuring transcription endpoints.",
    "Allow an empty transcription prompt to use the provider's native behavior.",
  ],
  fixes: ["Preserve unrecognized MOSS transcript responses instead of partially rewriting them."],
  breakingChanges: [],
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
