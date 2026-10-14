import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getUser, type UserResponse } from "@/lib/api/users";
import { getConfig } from "@/lib/api/system";
import { ApiError } from "@/lib/api/client";
import UserDetailPage from "./detail";

vi.mock("@/lib/api/users", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/users")>("@/lib/api/users");
  return { ...actual, getUser: vi.fn() };
});
vi.mock("@/lib/api/system", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/system")>("@/lib/api/system");
  return { ...actual, getConfig: vi.fn().mockResolvedValue({ rdb: { default_file_quota: 200 } }) };
});

const getUserMock = vi.mocked(getUser);

vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
);

function makeUser(): UserResponse {
  return {
    id: 12,
    display_name: "Ada Lovelace",
    external_user_id: "ada",
    email: "ada@example.test",
    is_admin: false,
    file_quota: 10,
    file_count: 2,
    created_at: null,
  };
}

function renderUserDetail(cachedUser = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (cachedUser) queryClient.setQueryData(["user", "12"], makeUser(), { updatedAt: 0 });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/users/12"]}>
        <Routes>
          <Route path="/users/:id" element={<UserDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UserDetailPage read errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getUserMock.mockReset();
    vi.mocked(getConfig).mockResolvedValue({ rdb: { default_file_quota: 200 } } as never);
  });

  it("keeps a retryable service failure distinct from a missing user", async () => {
    getUserMock.mockRejectedValueOnce(new Error("service unavailable")).mockResolvedValueOnce(makeUser());
    const user = userEvent.setup();
    renderUserDetail();

    expect(await screen.findByText("User could not be loaded")).not.toBeNull();
    expect(screen.queryByText("User not found.")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Ada Lovelace" })).not.toBeNull();
  });

  it("shows not found only when the API returns 404", async () => {
    getUserMock.mockRejectedValue(new ApiError(404, { detail: "User not found" }));
    renderUserDetail();

    expect(await screen.findByText("User not found.")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("keeps a 503 service error retryable instead of reporting a missing user", async () => {
    getUserMock.mockRejectedValue(new ApiError(503, { detail: "Service unavailable" }));
    renderUserDetail();

    expect(await screen.findByText("User could not be loaded")).not.toBeNull();
    expect(screen.queryByText("User not found.")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeNull();
  });

  it("keeps the loaded user visible and labels the profile stale after refresh fails", async () => {
    getUserMock.mockRejectedValue(new Error("service unavailable"));
    renderUserDetail(true);

    expect(await screen.findByText("User could not be refreshed")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Ada Lovelace" })).not.toBeNull();
  });
});
