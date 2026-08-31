/**
 * The single source of truth for the release notes shown in the Admin Console.
 * Update this object for each release; the sidebar label, unread state, and
 * dialog content are derived from it.
 */
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
  newFeatures: readonly string[];
}

export const releaseNotes: ReleaseNotes = {
  version: "2.2.0",
  date: "2026-08-31",
  summary:
    "OpenRAG 2.2 gives administrators more control over document chunking and speech-to-text configuration directly from the Admin Console.",
  breakingChanges: {
    title: "Breaking Changes",
    calloutTitle: "Milvus 3.0 migration required",
    description: "OpenRAG 2.2 requires Milvus 3.0. Existing Milvus 2.x deployments must be migrated before upgrading.",
    action: "Back up your data and follow the Milvus migration guide before starting the new version.",
  },
  newFeatures: [
    "Choose the new structured_section chunking strategy from indexation presets to keep document sections, headings, tables, and captions together.",
    "Register, validate, and choose OpenAI-compatible STT endpoints such as Whisper or MOSS from Model Endpoints.",
    "Create and update transcription prompts in the prompt library without restarting OpenRAG.",
    "Set an STT endpoint and transcription prompt per indexation preset, so each partition can use the appropriate speech-to-text behavior.",
    "Choose how MOSS diarized transcripts are formatted: raw output, timestamped speaker lines, or speaker-aware lines.",
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
