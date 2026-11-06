import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deleteUser, getUser, listUsers } from "@/lib/api/users";
import type { UserResponse } from "@/lib/api/users";
import UserListPage from "./list";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/lib/api/system", () => ({
  getConfig: vi.fn().mockResolvedValue({ rdb: { default_file_quota: 200 } }),
}));

vi.mock("@/lib/api/users", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/users")>("@/lib/api/users");
  return {
    ...actual,
    listUsers: vi.fn(),
    getUser: vi.fn(),
    deleteUser: vi.fn(),
    createUser: vi.fn(),
  };
});

const listUsersMock = vi.mocked(listUsers);
const getUserMock = vi.mocked(getUser);
const deleteUserMock = vi.mocked(deleteUser);

function makeUser(overrides: Partial<UserResponse> = {}): UserResponse {
  return {
    id: 2,
    display_name: "Ada Lovelace",
    external_user_id: "ada",
    email: "ada@example.test",
    is_admin: false,
    file_quota: null,
    file_count: 0,
    created_at: null,
    ...overrides,
  };
}

function renderUsers(cachedUsers?: UserResponse[], cachedUser?: UserResponse) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  if (cachedUsers) {
    queryClient.setQueryData(["users"], { users: cachedUsers });
  }
  if (cachedUser) {
    queryClient.setQueryData(["user", String(cachedUser.id)], cachedUser);
  }

  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UserListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("UserListPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listUsersMock.mockResolvedValue({
      users: [makeUser()],
    });
  });

  it("uses compact labelled row icon actions", async () => {
    renderUsers();

    const view = await screen.findByRole("link", { name: /view ada lovelace/i });
    const deleteAction = screen.getByRole("button", { name: /delete ada lovelace/i });

    expect(view.getAttribute("data-size")).toBe("icon-xs");
    expect(deleteAction.getAttribute("data-size")).toBe("icon-xs");
  });

  it("names the partitions that deleting a user also deletes", async () => {
    getUserMock.mockResolvedValue(
      makeUser({
        memberships: [
          { partition: "default", role: "owner", added_at: null },
          { partition: "research", role: "owner", added_at: null },
          { partition: "shared", role: "editor", added_at: null },
          { partition: "archive", role: "owner", added_at: null },
        ],
      }),
    );
    renderUsers();

    await userEvent.click(await screen.findByRole("button", { name: /delete ada lovelace/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(
      await within(dialog).findByText(
        /This also deletes the 2 partitions they own, with all their documents: research, archive\./,
      ),
    ).toBeTruthy();
    expect(dialog.textContent).toContain("The default partition is kept");
    expect(dialog.textContent).not.toContain("shared");
    expect(getUserMock).toHaveBeenCalledWith(2);
  });

  it("does not name partitions from a cached user while the fresh one is loading", async () => {
    getUserMock.mockReturnValue(new Promise(() => {}));
    renderUsers(undefined, makeUser({ memberships: [] }));

    await userEvent.click(await screen.findByRole("button", { name: /delete ada lovelace/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("Every partition they own is deleted too");
    expect(dialog.textContent).not.toContain("They own no partitions");
    expect(getUserMock).toHaveBeenCalledWith(2);
  });

  it("refetches the user when the dialog opens instead of trusting the cache", async () => {
    getUserMock.mockResolvedValue(
      makeUser({ memberships: [{ partition: "research", role: "owner", added_at: null }] }),
    );
    renderUsers(undefined, makeUser({ memberships: [] }));

    await userEvent.click(await screen.findByRole("button", { name: /delete ada lovelace/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(
      await within(dialog).findByText(/This also deletes the partition they own, with all its documents: research\./),
    ).toBeTruthy();
  });

  it("keeps the generic text when the user cannot be fetched", async () => {
    getUserMock.mockRejectedValue(new Error("boom"));
    renderUsers(undefined, makeUser({ memberships: [] }));

    await userEvent.click(await screen.findByRole("button", { name: /delete ada lovelace/i }));

    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(getUserMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(dialog.textContent).toContain("Every partition they own is deleted too");
    expect(dialog.textContent).not.toContain("They own no partitions");
  });

  it("forgets a deleted user's cached details", async () => {
    getUserMock.mockResolvedValue(makeUser({ memberships: [] }));
    deleteUserMock.mockResolvedValue(undefined);
    const { queryClient } = renderUsers(undefined, makeUser({ memberships: [] }));

    await userEvent.click(await screen.findByRole("button", { name: /delete ada lovelace/i }));
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(deleteUserMock).toHaveBeenCalled());
    await waitFor(() => expect(queryClient.getQueryData(["user", "2"])).toBeUndefined());
  });

  it("searches visible identifiers across paginated rows", async () => {
    const users = Array.from({ length: 10 }, (_, index) =>
      makeUser({
        id: index + 2,
        display_name: `User ${index + 2}`,
        external_user_id: `subject-${index + 2}`,
        email: `user-${index + 2}@example.test`,
      }),
    );
    users.push(
      makeUser({
        id: 12,
        display_name: "Zara Operator",
        external_user_id: "oidc-zara",
        email: "zara@example.test",
      }),
    );
    listUsersMock.mockResolvedValue({ users });

    renderUsers();

    const search = await screen.findByRole("searchbox", { name: "Search users" });
    expect(screen.queryByText("Zara Operator")).toBeNull();

    await userEvent.type(search, "ZARA@EXAMPLE.TEST");

    expect(await screen.findByText("Zara Operator")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("1 of 11 users");
    expect(listUsersMock).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Clear user search" }));
    await userEvent.type(search, "oidc-zara");

    expect(await screen.findByText("Zara Operator")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Clear user search" }));
    await userEvent.type(search, "zArA operator");

    expect(await screen.findByText("Zara Operator")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Clear user search" }));
    await userEvent.type(search, "User #12");

    expect(await screen.findByText("Zara Operator")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("1 of 11 users");
  });

  it("shows a clear no-result state and restores the list when search is cleared", async () => {
    renderUsers();

    const search = await screen.findByRole("searchbox", { name: "Search users" });
    await userEvent.type(search, "missing account");

    expect(screen.getByText("No users match “missing account”.")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Clear user search" }));

    expect(await screen.findByText("Ada Lovelace")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("1 user");
  });

  it("returns to the first page when search changes or is cleared", async () => {
    const users = Array.from({ length: 30 }, (_, index) =>
      makeUser({
        id: index + 2,
        display_name: `Match ${index + 1}`,
        external_user_id: `subject-${index + 2}`,
        email: `user-${index + 2}@example.test`,
      }),
    );
    listUsersMock.mockResolvedValue({ users });

    renderUsers();

    expect(await screen.findByText("Page 1 of 3")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("Page 3 of 3")).toBeTruthy();

    const search = screen.getByRole("searchbox", { name: "Search users" });
    await userEvent.type(search, "match");

    expect(await screen.findByText("Page 1 of 3")).toBeTruthy();
    expect(screen.getByText("Match 1")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Clear user search" }));

    expect(await screen.findByText("Page 1 of 3")).toBeTruthy();
    expect(screen.getByText("Match 1")).toBeTruthy();
  });

  it("distinguishes an empty directory from an unsuccessful search", async () => {
    listUsersMock.mockResolvedValue({ users: [] });

    renderUsers();

    expect(await screen.findByText("No users have been created yet.")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("0 users");
  });

  it("shows a retryable error instead of reporting a failed request as an empty directory", async () => {
    listUsersMock.mockRejectedValueOnce(new Error("User service unavailable"));

    renderUsers();

    expect((await screen.findByRole("alert")).textContent).toContain("Users could not be loaded");
    expect(screen.getByRole("alert").textContent).toContain("User service unavailable");
    expect(screen.queryByText("No users have been created yet.")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Ada Lovelace")).toBeTruthy();
  });

  it("keeps cached users visible when a background refresh fails", async () => {
    listUsersMock.mockRejectedValueOnce(new Error("Refresh unavailable"));

    renderUsers([makeUser()]);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Users could not be refreshed");
    expect(alert.textContent).toContain("Showing previously loaded users");
    expect(screen.getByText("Ada Lovelace")).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: "Search users" })).toBeTruthy();
  });
});
