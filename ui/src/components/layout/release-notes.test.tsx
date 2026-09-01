import { render, screen, waitFor } from "@testing-library/react";
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
    expect(dialog.textContent).toContain("Latest release");
    expect(dialog.textContent).toContain("Released August 31, 2026");
    expect(dialog.textContent).toContain("What's New");
    expect(dialog.textContent).toContain("New Features");
    expect(dialog.textContent).toContain("Breaking Changes");
    expect(dialog.textContent).toContain("Milvus 3.0 migration required");
    expect(dialog.textContent).toContain("structured_section chunking strategy");
    expect(dialog.textContent).toContain("OpenAI-compatible STT endpoints");
    expect(dialog.textContent).toContain("transcription prompts in the prompt library");
    expect(dialog.textContent).toContain("per indexation preset");
    expect(dialog.textContent).toContain("MOSS diarized transcripts");
    expect(screen.getByRole("heading", { name: "What's New" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "New Features" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Fixes" })).toBeNull();
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

    const button = screen.getByRole("button", { name: new RegExp(`Open Release Notes · v${releaseNotes.version}`) });
    await user.click(button);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(button));
  });
});
