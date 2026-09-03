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
  version: "2.2.0",
  date: "2026-09-03",
  summary:
    "OpenRAG 2.2.0 expands retrieval, indexing, chunking, and speech-to-text capabilities, while improving administration and release visibility.",

  newFeatures: [
    "Scope chat retrieval to specific indexed files by providing attachment file IDs, so answers can focus only on the selected documents.",
    "Provide a custom system prompt with chat requests while preserving OpenRAG's core RAG instructions and security rules.",
    "Receive an optional callback when asynchronous file indexing completes or fails, with support for authenticated callback endpoints.",
    "Choose the new structured_section chunking strategy from indexation presets to keep document sections, headings, tables, and captions together.",
    "Register, validate, and choose OpenAI-compatible STT endpoints such as Whisper or MOSS directly from Model Endpoints.",
    "Create and manage ASR transcription prompts from the prompt library without restarting OpenRAG.",
    "Set an STT endpoint and transcription prompt per indexation preset, allowing different partitions to use different speech-to-text configurations.",
    "Normalize MOSS diarized transcription output into cleaner speaker-aware text suitable for indexing and retrieval.",
    "See active indexing Jobs directly from the sidebar with a live job-count indicator.",
    "Discover recently introduced capabilities through temporary New badges that automatically expire after their release window.",
    "Read the highlights and migration requirements for the current OpenRAG release from the new Release Notes dialog in the Admin Console.",
    "BM25 lexical retrieval is now case-insensitive, improving matches when query and document capitalization differ.",
    "New STT, transcription prompt, MOSS speaker-aware, preset, and Jobs capabilities are highlighted in the Admin Console for easier discovery.",
  ],

  breakingChange: {
    title: "Milvus 3.0 migration required",
    description:
      "OpenRAG 2.2.0 requires Milvus 3.0. Existing Milvus 2.x deployments must be migrated before upgrading. The BM25 analyzer has also changed to support case-insensitive lexical search and requires the corresponding vector database schema migration.",
    action:
      "Back up your Milvus data and follow the Milvus migration guide, including the required schema migrations, before starting OpenRAG 2.2.0.",
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
