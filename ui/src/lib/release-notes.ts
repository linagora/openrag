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
  version: "2.3.3",
  date: "2026-10-28",
  summary:
    "OpenRAG 2.3.3 improves indexing for CSV and multilingual documents, adds controls for image captioning, and tightens access to the Ray dashboard in Docker Compose.",

  newFeatures: [
    "CSV files are indexed in batches, including wide rows and large cells, so their contents remain searchable without creating oversized chunks.",
    "Indexing detects a document's language from text sampled across the file, improving language selection for multilingual and long documents.",
    "VLM endpoints can set max_image_side to reduce image dimensions before captioning, helping limit the image sent to vision models.",
    "Ollama embedding requests are batched for more efficient embedding generation.",
    "Document details can display topic tags.",
    "CSV parsing handles mixed encodings and stray bytes more reliably, and oversized chunks or metadata updates are handled without failing the whole file.",
    "URL-encoded request bodies are bounded while they are read, and URL host checks normalize hostnames before applying the safety rules.",
  ],

  breakingChange: {
    title: "Docker Compose no longer publishes the Ray dashboard port by default",
    description:
      "OpenRAG 2.3.3 removes the Docker Compose port mapping for 8265. With the default RAY_DASHBOARD_HOST, the mapping did not reach the dashboard, and the Ray dashboard API is unauthenticated.",
    action:
      "To access the dashboard with Docker Compose, add ray-dashboard.docker-compose.yaml last to the Compose command, after docker-compose.yaml and any other overlays. Expose it only on a private network or behind an authenticating proxy.",
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
