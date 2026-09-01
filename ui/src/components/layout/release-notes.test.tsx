import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { formatReleaseDate, LAST_VIEWED_RELEASE_NOTES_KEY, releaseNotes } from "@/lib/release-notes";
import { ReleaseNotes } from "./release-notes";

const triggerLabel = `Open Release Notes · v${releaseNotes.version}`;

describe("ReleaseNotes", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("opens the current release without navigating and remembers it as viewed", async () => {
    const user = userEvent.setup();
    render(<ReleaseNotes />);

    const button = screen.getByRole("button", { name: `${triggerLabel}, new` });
    expect(button.textContent).toContain(`Release Notes · v${releaseNotes.version}`);
    expect(button.textContent).toContain("New");

    await user.click(button);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: `OpenRAG v${releaseNotes.version}` })).toBeTruthy();
    expect(within(dialog).getByRole("heading", { name: "What's New" })).toBeTruthy();
    expect(within(dialog).getByRole("heading", { name: "New Features" })).toBeTruthy();
    expect(dialog.textContent).toContain(releaseNotes.summary);
    expect(dialog.textContent).toContain(`Released ${formatReleaseDate(releaseNotes.date)}`);
    expect(within(dialog).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      ...releaseNotes.newFeatures,
    ]);

    expect(localStorage.getItem(LAST_VIEWED_RELEASE_NOTES_KEY)).toBe(releaseNotes.version);
    expect(button.textContent).not.toContain("New");
  });

  it("renders the breaking-change callout from the release data", async () => {
    const user = userEvent.setup();
    const { breakingChange } = releaseNotes;
    expect(breakingChange).toBeTruthy();
    render(<ReleaseNotes />);

    await user.click(screen.getByRole("button", { name: /^Open Release Notes/ }));

    const dialog = await screen.findByRole("dialog");
    const callout = within(dialog).getByRole("alert");
    expect(callout.textContent).toContain("Breaking Changes");
    if (breakingChange) {
      expect(callout.textContent).toContain(breakingChange.title);
      expect(callout.textContent).toContain(breakingChange.description);
      expect(callout.textContent).toContain(breakingChange.action);
    }
  });

  it("does not show the new badge after the current version was viewed", () => {
    localStorage.setItem(LAST_VIEWED_RELEASE_NOTES_KEY, releaseNotes.version);
    render(<ReleaseNotes />);

    expect(screen.getByRole("button", { name: triggerLabel }).textContent).not.toContain("New");
  });

  it("closes with Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    render(<ReleaseNotes />);

    const button = screen.getByRole("button", { name: /^Open Release Notes/ });
    await user.click(button);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(button));
  });
});
