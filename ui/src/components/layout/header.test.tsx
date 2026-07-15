import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Header } from "./header";
import { TOKEN_KEY } from "@/lib/api/client";

const authState = vi.hoisted(() => ({
  chainlitEnabled: true,
  logout: vi.fn(() => false),
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
    logout: authState.logout,
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
    authState.logout.mockImplementation(() => false);
    fetchMock.mockResolvedValue(fakeResponse());
  });

  afterEach(() => {
    vi.useRealTimers();
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
      expect(openedWindow.location.href).toBe("/chainlit/");
    });
    expect(openMock).toHaveBeenCalledWith("about:blank", "_blank");
    expect(openedWindow.opener).toBeNull();
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

  it("opens chat after the handoff timeout if the session request stalls", async () => {
    vi.useFakeTimers();
    const openedWindow = { opener: {}, location: { href: "" } };
    openMock.mockReturnValue(openedWindow);
    fetchMock.mockReturnValue(new Promise(() => undefined));

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /open chat in a new tab/i }));

    expect(openedWindow.location.href).toBe("");

    await vi.advanceTimersByTimeAsync(3000);

    expect(openedWindow.location.href).toBe("/chainlit/");
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

  it("clears the Chainlit handoff cookie before token logout", async () => {
    localStorage.setItem(TOKEN_KEY, "or-user-token");
    authState.logout.mockImplementation(() => {
      localStorage.removeItem(TOKEN_KEY);
      return false;
    });

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /log out/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/auth/chainlit-session",
        expect.objectContaining({
          method: "DELETE",
          headers: expect.objectContaining({ Authorization: "Bearer or-user-token" }),
        }),
      );
    });
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("does not wait for Chainlit cookie cleanup before local token logout", () => {
    localStorage.setItem(TOKEN_KEY, "or-user-token");
    authState.logout.mockImplementation(() => {
      localStorage.removeItem(TOKEN_KEY);
      return false;
    });
    fetchMock.mockReturnValue(new Promise(() => undefined));

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /log out/i }));

    expect(authState.logout).toHaveBeenCalled();
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});
