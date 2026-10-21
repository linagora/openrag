import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  formatReleaseDate,
  hasViewedRelease,
  LAST_VIEWED_RELEASE_NOTES_KEY,
  markReleaseAsViewed,
} from "./release-notes";

describe("release notes storage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("tracks the viewed version rather than hiding future release notes", () => {
    markReleaseAsViewed("2.2.0");

    expect(localStorage.getItem(LAST_VIEWED_RELEASE_NOTES_KEY)).toBe("2.2.0");
    expect(hasViewedRelease("2.2.0")).toBe(true);
    expect(hasViewedRelease("2.3.0")).toBe(false);
  });

  it("formats the ISO release date for the dialog", () => {
    expect(formatReleaseDate("2026-08-31")).toBe("August 31, 2026");
  });
});
