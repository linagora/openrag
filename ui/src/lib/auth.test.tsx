import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import type { ReactNode } from "react";
import { AuthProvider, useAuth } from "./auth";

// Identity is resolved from /users/info; mock that call.
vi.mock("./api/account", () => ({ getMyInfo: vi.fn() }));
import { getMyInfo, type MyInfo } from "./api/account";

const mockInfo = getMyInfo as unknown as ReturnType<typeof vi.fn>;
const wrapper = ({ children }: { children: ReactNode }) => <AuthProvider>{children}</AuthProvider>;

function user(is_admin: boolean): MyInfo {
  return { id: 1, display_name: "Test", is_admin, file_quota: null };
}

afterEach(() => {
  localStorage.clear();
  document.cookie = "openrag_chainlit_logout=; Max-Age=0; path=/";
  vi.clearAllMocks();
});

describe("useAuth (token model)", () => {
  it("isAdmin is true when /users/info reports is_admin", async () => {
    mockInfo.mockResolvedValue(user(true));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.isAdmin).toBe(true);
  });

  it("isAdmin is false for a non-admin user", async () => {
    mockInfo.mockResolvedValue(user(false));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.isAdmin).toBe(false);
  });

  it("is unauthenticated when /users/info rejects (no valid token/session)", async () => {
    mockInfo.mockRejectedValue(new Error("401"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.isAdmin).toBe(false);
  });

  it("loginWithToken stores the token and resolves identity", async () => {
    mockInfo.mockRejectedValueOnce(new Error("401")); // initial probe: not signed in
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isAuthenticated).toBe(false);

    mockInfo.mockResolvedValueOnce(user(true)); // token accepted
    await act(async () => {
      await result.current.loginWithToken("or-abc123");
    });
    expect(localStorage.getItem("openrag_token")).toBe("or-abc123");
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.isAdmin).toBe(true);
  });

  it("logout clears the token and user", async () => {
    mockInfo.mockResolvedValue(user(true));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true));

    act(() => result.current.logout());
    expect(localStorage.getItem("openrag_token")).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("logout returns false in token mode (a bearer token is stored)", async () => {
    localStorage.setItem("openrag_token", "or-abc123");
    mockInfo.mockResolvedValue(user(true));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true));

    let wasOidc: boolean | undefined;
    act(() => {
      wasOidc = result.current.logout();
    });
    // Token mode: clearing the local token IS the full logout — no server redirect.
    expect(wasOidc).toBe(false);
    expect(localStorage.getItem("openrag_token")).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("logout returns true for an OIDC session (no stored token) so the caller revokes it server-side", async () => {
    // No token in localStorage → identity came from the OIDC `openrag_session`
    // cookie, which JS can't clear; the header must hand off to GET /auth/logout.
    mockInfo.mockResolvedValue(user(true));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true));

    let wasOidc: boolean | undefined;
    act(() => {
      wasOidc = result.current.logout();
    });
    expect(wasOidc).toBe(true);
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("clears token auth when Chainlit logout signal is present on load", async () => {
    localStorage.setItem("openrag_token", "or-abc123");
    document.cookie = "openrag_chainlit_logout=1; path=/";
    mockInfo.mockResolvedValue(user(true));

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(localStorage.getItem("openrag_token")).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(document.cookie).not.toContain("openrag_chainlit_logout=1");
    expect(mockInfo).not.toHaveBeenCalled();
  });

  it("clears an already-open Admin UI tab after Chainlit logout", async () => {
    localStorage.setItem("openrag_token", "or-abc123");
    mockInfo.mockResolvedValue(user(true));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true));

    document.cookie = "openrag_chainlit_logout=1; path=/";
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(localStorage.getItem("openrag_token")).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(document.cookie).not.toContain("openrag_chainlit_logout=1");
  });
});
