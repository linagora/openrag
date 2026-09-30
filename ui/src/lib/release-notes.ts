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
  version: "2.3.0",
  date: "2026-09-30",
  summary:
    "OpenRAG 2.3.0 makes deployments observable and more robust: durable job history, dependency-aware readiness, metrics, alerts and dashboards, and one vector field per embedder so partitions can use different embedding models.",

  newFeatures: [
    "Indexing job status and history are stored in PostgreSQL and survive restarts.",
    "Degraded indexing outcomes and failure reasons are shown in the Jobs view.",
    "Each embedder has its own vector field: partitions can use different embedding models, and each partition is searched with its own.",
    "A partition's embedder is validated when the partition is created or updated, and editing the URL or model of an embedder that already has indexed files requires explicit acknowledgement.",
    "New deployments chunk documents with the structure-aware structured_section strategy by default; existing presets keep their chunker.",
    "Uploaded files are checked against their declared extension, and a second upload of a file that is still indexing is refused.",
    "Documents that produce no chunks now fail with a clear reason instead of being reported as indexed.",
    "Workspace deletion can keep the workspace's files, and workspace IDs only need to be unique within a partition.",
    "Chat sources report the reranker score of each document when a reranker ran.",
    "A new /ready endpoint reports PostgreSQL, Milvus, Ray and the model endpoints in use.",
    "Prometheus metrics, alert rules with runbooks and Grafana dashboards ship with the Helm chart and Docker Compose.",
    "Logs can be written as JSON with a request ID on every line and shipped to Loki.",
  ],

  breakingChange: {
    title: "Milvus collection migration and configuration changes required",
    description:
      "OpenRAG 2.3.0 migrates its Milvus collection to schema version 3, which moves each embedder's vectors into its own field. Milvus itself moves to v3.0.2, which is required for that migration. Until the migration runs, every search fails. Chat responses now return extra as a JSON object with chunk metadata nested under chunk, JSON requests need a Content-Type: application/json header, GET /metrics needs METRICS_TOKEN, and example credentials are refused at startup.",
    action:
      "Plan a maintenance window and follow the Upgrading OpenRAG guide for 2.2.x to 2.3.0, including backups, secrets, the Milvus migration and the API client changes, before starting OpenRAG 2.3.0.",
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
