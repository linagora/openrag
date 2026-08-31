import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { LAST_VIEWED_RELEASE_NOTES_KEY, releaseNotes } from "@/lib/release-notes";
import { ReleaseNotes } from "./release-notes";

function renderReleaseNotes() {
  return render(
    <ul>
      <ReleaseNotes className="release-notes-nav-item" />
    </ul>,
  );
}

describe("ReleaseNotes", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("opens the current release without navigating and remembers it as viewed", async () => {
    const user = userEvent.setup();
    renderReleaseNotes();

    const button = screen.getByRole("button", { name: new RegExp(`Open Release Notes · v${releaseNotes.version}`) });
    expect(button.textContent).toContain(`Release Notes · v${releaseNotes.version}`);
    expect(button.textContent).toContain("New");

    await user.click(button);

    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain(`OpenRAG v${releaseNotes.version}`);
    expect(dialog.textContent).toContain("Released: August 31, 2026");
    expect(dialog.textContent).toContain("What's New");
    expect(screen.queryByRole("heading", { name: "Improvements" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Bug Fixes" })).toBeNull();
    expect(localStorage.getItem(LAST_VIEWED_RELEASE_NOTES_KEY)).toBe(releaseNotes.version);
    expect(button.textContent).not.toContain("New");
  });

  it("does not show the new badge after the current version was viewed", () => {
    localStorage.setItem(LAST_VIEWED_RELEASE_NOTES_KEY, releaseNotes.version);
    renderReleaseNotes();

    expect(screen.getByRole("button", { name: `Open Release Notes · v${releaseNotes.version}` }).textContent).not.toContain("New");
  });

  it("closes with Escape", async () => {
    const user = userEvent.setup();
    renderReleaseNotes();

    await user.click(screen.getByRole("button", { name: new RegExp(`Open Release Notes · v${releaseNotes.version}`) }));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
