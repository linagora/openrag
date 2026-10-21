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
  version: "2.3.2",
  date: "2026-10-20",
  summary:
    "OpenRAG 2.3.2 lets a partition move to another embedding model without re-uploading its files, adds reranking to search and operator-managed accounts, and keeps a single bad parse from taking indexing down with it.",

  newFeatures: [
    "A partition can be re-embedded into another embedder from its page, without re-uploading its files, and files indexed with another model can be re-embedded in place.",
    "The search endpoints can rerank their results with rerank=true, and return each document's rerank score.",
    "Service accounts and their tokens can be declared in the configuration (auth.seed_users, or openrag.seedUsers in the Helm chart) and are provisioned at startup.",
    "Default prompts that were never edited are refreshed to the bundled template at startup; edited defaults and named prompts are kept.",
    "An experimental, admin-only diagnostics endpoint traces contextualization, retrieval and reranking without generating an answer.",
    "PDFs can be parsed in parallel child processes, and a PyMuPDF or Marker parse can be given a memory ceiling, so an oversized file fails on its own instead of stopping the worker.",
    "A failed indexing job names the underlying error, and a file that stores nothing says which stage left it empty.",
    "The default partition can no longer be deleted.",
    "The sign-in page keeps the entered token and offers a retry when the service is unreachable, instead of reporting the token as invalid.",
    "Docker Compose has an opt-in TLS reverse proxy overlay, proxy.docker-compose.yaml.",
    "Tracebacks in the logs no longer print local variables, which could expose tokens and other secrets.",
  ],

  breakingChange: {
    title: "Object storage, prompt and model setting changes",
    description:
      "OpenRAG 2.3.2 replaces the bundled MinIO with SILO, a switch that cannot be undone, and the Helm chart now requires MinIO credentials. Untouched default prompts are refreshed at startup. enable_thinking: false now reaches the model, and a model with a Mistral tokenizer rejects it. The Helm chart refuses PostgreSQL migration settings that apply no migrations, and the reranker's model and port each take a single value.",
    action:
      "Back up the MinIO, etcd and Milvus data, then follow the 2.3.0 or 2.3.1 to a later 2.3.x section of the Upgrading OpenRAG guide before starting OpenRAG 2.3.2. From 2.2.x, follow the 2.2.x to 2.3.0 guide first.",
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
