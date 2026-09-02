export interface ReleaseBreakingChange {
  title: string;
  description: string;
  action: string;
}

export interface Release {
  version: string;
  date: string;
  summary: string;
  newFeatures: readonly string[];
  breakingChange?: ReleaseBreakingChange;
}

/**
 * The single source of truth for the release notes shown in the Admin Console.
 * Update this object for each release; the sidebar label, unread state, and
 * dialog content are derived from it.
 */
export const releaseNotes: Release = {
  // Placeholder — the real version and date are set on the release/X.Y.Z
  // branch when the version is actually chosen. See CONTRIBUTING.md § Writing
  // the release notes.
  version: "2.X.X",
  date: "2026-01-01",
  summary:
    "OpenRAG 2.X.X gives administrators more control over document chunking and speech-to-text configuration directly from the Admin Console.",
  newFeatures: [
    "Choose the new structured_section chunking strategy from indexation presets to keep document sections, headings, tables, and captions together.",
    "Register, validate, and choose OpenAI-compatible STT endpoints such as Whisper or MOSS from Model Endpoints.",
    "Create and update transcription prompts in the prompt library without restarting OpenRAG.",
    "Set an STT endpoint and transcription prompt per indexation preset, so each partition can use the appropriate speech-to-text behavior.",
    "Choose how MOSS diarized transcripts are formatted: raw output, timestamped speaker lines, or speaker-aware lines.",
    "Read what shipped in each release from the new Release Notes entry in the sidebar.",
  ],
  breakingChange: {
    title: "Milvus 3.0 migration required",
    description: "OpenRAG 2.X.X requires Milvus 3.0. Existing Milvus 2.x deployments must be migrated before upgrading.",
    action: "Back up your data and follow the Milvus migration guide before starting the new version.",
  },
};

export const LAST_VIEWED_RELEASE_NOTES_KEY = "openrag:last-viewed-release-notes-version";

export function hasViewedRelease(version: string): boolean {
  try {
    return localStorage.getItem(LAST_VIEWED_RELEASE_NOTES_KEY) === version;
  } catch {
    // Local storage can be disabled by the browser. The dialog remains usable;
    // the badge simply stays visible until storage is available again.
    return false;
  }
}

export function markReleaseAsViewed(version: string): void {
  try {
    localStorage.setItem(LAST_VIEWED_RELEASE_NOTES_KEY, version);
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
