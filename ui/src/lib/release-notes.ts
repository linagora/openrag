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
  version: "2.2.2",
  date: "2026-09-24",
  summary:
    "OpenRAG 2.2.2 restores document retrieval for factual chat requests that could previously receive an answer without sources. Casual introductions are neutral by default and can use a deployment-specific assistant name.",

  newFeatures: [
    "Factual and ambiguous chat requests now search indexed documents by default, even when an older stored contextualizer prompt returns no search query. Text completions keep their existing opt-in behavior.",
    "Greetings can introduce your assistant using ASSISTANT_NAME. Without it, the introduction contains no vendor name.",
  ],

  breakingChange: {
    title: "Milvus 3.0 migration for older installations",
    description:
      "OpenRAG 2.2.2 adds no new migration. Installations upgrading directly from 2.2.0 or earlier still need the Milvus 3.0 and BM25 schema migration introduced in 2.2.1.",
    action:
      "Back up Milvus and complete the 2.2.1 migration guide before starting 2.2.2. No additional migration is needed when upgrading from 2.2.1.",
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
