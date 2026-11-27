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
  version: "2.3.6",
  date: "2026-11-30",
  summary:
    "OpenRAG 2.3.6 indexes OpenDocument files (.odt, .odp) and legacy Excel workbooks (.xls), adds workspaces to the admin console, and makes large files, file quotas and the bundled proxy more reliable.",

  newFeatures: [
    "Workspaces can be created and managed in the admin console, and a chat can be scoped to one.",
    "OpenDocument text and presentation files (.odt, .odp) and legacy Excel workbooks (.xls) can now be indexed, and charts in .xlsx workbooks become searchable text.",
    "The model endpoint form lists the models the endpoint serves.",
    "Large files no longer fail when their chunks are written to Milvus, and file quotas hold when several uploads arrive at once.",
    "The Jobs page shows running jobs' stage in every tab, and a partial batch upload keeps its failed files listed.",
  ],
  breakingChange: {
    title: "Docker Compose: recreate the network and check credentials before upgrading",
    description:
      "Compose now trusts only the bundled admin UI proxy for forwarded client addresses, which requires recreating the project network once. Service credentials are checked before PostgreSQL, MinIO and Milvus start: an empty one stops the service, a published or too short one stops it unless ALLOW_INSECURE_SECRETS=true. Compose reads the API's environment file from OPENRAG_ENV_FILE and ignores SHARED_ENV. The default similarity threshold is now 0.2.",
    action:
      "Read the 2.3.6 sections of the upgrade guide before upgrading. On Compose, run docker compose down then docker compose up -d once, with the same files and profiles and without -v. On a shared Ray cluster, stop uploads and wait for queued indexing to finish first.",
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
