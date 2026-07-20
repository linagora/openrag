import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentAuthInfo, regenerateMyToken } from "@/lib/api/account";
import SettingsPage from "./settings";

vi.mock("@/lib/api/account", () => ({
  getCurrentAuthInfo: vi.fn(),
  regenerateMyToken: vi.fn(),
}));

const getCurrentAuthInfoMock = vi.mocked(getCurrentAuthInfo);
const regenerateMyTokenMock = vi.mocked(regenerateMyToken);

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

describe("SettingsPage authentication copy", () => {
  beforeEach(() => {
    getCurrentAuthInfoMock.mockReset();
    regenerateMyTokenMock.mockReset();
    regenerateMyTokenMock.mockResolvedValue({ token: "or-new-token" });
  });

  it("explains bearer token authentication in token mode", async () => {
    getCurrentAuthInfoMock.mockResolvedValue({
      user_id: 1,
      email: "admin@example.com",
      auth_method: "token",
      session_expires_at: null,
    });

    renderSettings();

    expect(await screen.findByText(/Authentication uses your OpenRAG bearer token/i)).toBeTruthy();
    expect(screen.queryByText(/organization's SSO/i)).toBeNull();
  });

  it("keeps the SSO explanation in OIDC mode", async () => {
    getCurrentAuthInfoMock.mockResolvedValue({
      user_id: 1,
      email: "admin@example.com",
      auth_method: "oidc",
      session_expires_at: "2026-07-16T10:00:00Z",
    });

    renderSettings();

    expect(await screen.findByText(/organization's SSO/i)).toBeTruthy();
    expect(screen.queryByText(/Authentication uses your OpenRAG bearer token/i)).toBeNull();
  });
});
