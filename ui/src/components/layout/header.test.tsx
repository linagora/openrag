import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Header } from "./header";
import { TOKEN_KEY } from "@/lib/api/client";

const authState = vi.hoisted(() => ({
  chainlitEnabled: true,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      display_name: "Admin User",
      email: "admin@example.com",
      is_admin: true,
      chainlit_enabled: authState.chainlitEnabled,
    },
    logout: vi.fn(),
  }),
}));

vi.mock("@/components/ui/sidebar", () => ({
  SidebarTrigger: () => <button type="button">Toggle sidebar</button>,
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

function fakeResponse(status = 204): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    text: async () => "",
    json: async () => ({}),
  } as unknown as Response;
}

describe("Header", () => {
  const fetchMock = vi.fn();
  const openMock = vi.fn();

  beforeEach(() => {
    authState.chainlitEnabled = true;
    vi.stubEnv("VITE_API_BASE_URL", "");
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("open", openMock);
    localStorage.clear();
    fetchMock.mockReset();
    openMock.mockReset();
    fetchMock.mockResolvedValue(fakeResponse());
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("prepares a Chainlit session before opening chat", async () => {
    const openedWindow = { opener: {}, location: { href: "" } };
    openMock.mockReturnValue(openedWindow);
    localStorage.setItem(TOKEN_KEY, "or-user-token");

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /open chat in a new tab/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/auth/chainlit-session",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer or-user-token" }),
        }),
      );
    });
    expect(openMock).toHaveBeenCalledWith("about:blank", "_blank");
    expect(openedWindow.opener).toBeNull();
    expect(openedWindow.location.href).toBe("/chainlit/");
  });

  it("uses the configured API origin for Chainlit in browser-direct builds", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.test");
    const openedWindow = { opener: {}, location: { href: "" } };
    openMock.mockReturnValue(openedWindow);

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /open chat in a new tab/i }));

    await waitFor(() => {
      expect(openedWindow.location.href).toBe("https://api.example.test/chainlit/");
    });
  });

  it("hides the Chainlit chat link when Chainlit is disabled", () => {
    authState.chainlitEnabled = false;

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /open chat in a new tab/i })).toBeNull();
  });
});
