import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemberPicker } from "./member-picker";
import type { PartitionMemberCandidate } from "@/lib/api/partitions";

const candidates = [
  { user_id: 2, display_name: "Sam Lee", email: "sam.lee@example.com" },
  { user_id: 3, display_name: "Sam Lee", email: "sam.lee@linagora.com" },
  { user_id: 4, display_name: null, email: null },
];

function renderPicker(
  overrides: Partial<React.ComponentProps<typeof MemberPicker>> = {},
) {
  const props: React.ComponentProps<typeof MemberPicker> = {
    candidates,
    isLoading: false,
    isInitialError: false,
    isRefreshError: false,
    isLoadMoreError: false,
    search: "Sam",
    searchReady: true,
    onSearchChange: vi.fn(),
    onRetry: vi.fn(),
    hasMore: false,
    isLoadingMore: false,
    onLoadMore: vi.fn(),
    selectedCandidates: [],
    onSelectionChange: vi.fn(),
    ...overrides,
  };
  return { ...render(<MemberPicker {...props} />), props };
}

describe("MemberPicker", () => {
  it("shows email addresses so duplicate names remain distinguishable", () => {
    renderPicker();

    expect(screen.getAllByText("Sam Lee")).toHaveLength(2);
    expect(screen.getByText("sam.lee@example.com")).not.toBeNull();
    expect(screen.getByText("sam.lee@linagora.com")).not.toBeNull();
    expect(screen.getByText("Unnamed user")).not.toBeNull();
    expect(screen.getByText("User ID 4")).not.toBeNull();
  });

  it("forwards search changes to the server-backed query", async () => {
    const onSearchChange = vi.fn();
    const user = userEvent.setup();
    renderPicker({ search: "", searchReady: false, onSearchChange });

    const search = screen.getByRole("textbox", { name: "Users" });
    await user.type(search, "3");

    expect(onSearchChange).toHaveBeenCalledWith("3");
  });

  it("returns selected identities when a candidate is checked", async () => {
    const onSelectionChange = vi.fn();
    const user = userEvent.setup();
    renderPicker({ selectedCandidates: [candidates[0]], onSelectionChange });

    await user.click(screen.getByRole("checkbox", { name: /select sam lee, sam.lee@linagora.com/i }));

    expect(onSelectionChange).toHaveBeenCalledWith([candidates[0], candidates[1]]);
  });

  it("loads the next server page on request", async () => {
    const onLoadMore = vi.fn();
    const user = userEvent.setup();
    renderPicker({ hasMore: true, onLoadMore });

    await user.click(screen.getByRole("button", { name: "Load more users" }));

    expect(onLoadMore).toHaveBeenCalledOnce();
  });

  it("keeps selected identities visible when they are absent from current results", async () => {
    const selected: PartitionMemberCandidate = {
      user_id: 9,
      display_name: "Alex Morgan",
      email: "alex@example.com",
    };
    const onSelectionChange = vi.fn();
    const user = userEvent.setup();
    renderPicker({
      candidates: [candidates[0]],
      selectedCandidates: [selected],
      onSelectionChange,
    });

    expect(screen.getByRole("region", { name: "Selected users" })).not.toBeNull();
    expect(screen.getByText("Alex Morgan")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /remove alex morgan, alex@example.com/i }));

    expect(onSelectionChange).toHaveBeenCalledWith([]);
  });

  it("asks for a targeted search before showing candidates", () => {
    renderPicker({ search: "Sa", searchReady: false });

    expect(screen.getByText("Enter at least 3 characters or an exact user ID.")).not.toBeNull();
    expect(screen.queryByText("Sam Lee")).toBeNull();
  });

  it("keeps cached candidates visible after a refresh error", () => {
    renderPicker({ isRefreshError: true });

    expect(screen.getByText(/previous results remain available/i)).not.toBeNull();
    expect(screen.getAllByText("Sam Lee")).toHaveLength(2);
  });

  it("offers a page-specific retry without discarding loaded candidates", async () => {
    const onLoadMore = vi.fn();
    const user = userEvent.setup();
    renderPicker({ isLoadMoreError: true, onLoadMore });

    expect(screen.getByText(/retry without losing this page/i)).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry loading more" }));

    expect(onLoadMore).toHaveBeenCalledOnce();
  });
});
