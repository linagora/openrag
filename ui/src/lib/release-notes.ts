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
  version: "2.3.5",
  date: "2026-11-12",
  summary:
    'OpenRAG 2.3.5 shows each file\'s indexing progress, adds sheet-aware spreadsheet ingestion and date-aware answers for "the latest" questions, and upgrades Milvus to 3.0.5.',

  newFeatures: [
    "The Jobs page shows each file's current indexing stage, its elapsed time and its stage history.",
    "Spreadsheets (.xlsx) are ingested sheet by sheet, without rejecting valid workbooks.",
    "Questions about the latest documents get sources sorted by date, with their dates shown.",
    "Figure and table captions stay attached to the block they label.",
    "Admin console usability improvements, and failed data loads shown as errors instead of empty lists.",
  ],
  breakingChange: {
    title: "Remote images in Markdown files are no longer captioned",
    description:
      "An image a Markdown file links to by URL (![alt](https://...)) is no longer fetched or captioned; its link stays in the text. Images embedded in the file are still captioned. The IMAGE_CAPTIONING_URL setting is removed, and a configuration that still sets it keeps loading and ignores it.",
    action:
      "If you relied on captions of linked images, embed those images in the Markdown file, or convert the document to a format whose images are embedded, such as PDF or DOCX, and index it again.",
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
