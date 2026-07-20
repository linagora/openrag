import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listUsers } from "@/lib/api/users";
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
    deleteUser: vi.fn(),
    createUser: vi.fn(),
  };
});

const listUsersMock = vi.mocked(listUsers);

function renderUsers() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UserListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UserListPage", () => {
  beforeEach(() => {
    listUsersMock.mockResolvedValue({
      users: [
        {
          id: 2,
          display_name: "Ada Lovelace",
          external_user_id: "ada",
          email: "ada@example.test",
          is_admin: false,
          file_quota: null,
          file_count: 0,
          created_at: null,
        },
      ],
    });
  });

  it("uses compact labelled row icon actions", async () => {
    renderUsers();

    const view = await screen.findByRole("link", { name: /view ada lovelace/i });
    const deleteAction = screen.getByRole("button", { name: /delete ada lovelace/i });

    expect(view.getAttribute("data-size")).toBe("icon-xs");
    expect(deleteAction.getAttribute("data-size")).toBe("icon-xs");
  });
});
