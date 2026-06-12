import { describe, it, expect, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { AuthProvider, useAuth } from "./auth";

// Helper to create a wrapper with a real AuthProvider but pre-loaded token
// We bypass the real login flow by injecting a fake JWT into localStorage.
function makeJwt(payload: Record<string, string>): string {
  const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.fakesig`;
}

function wrapperWithRole(role: string | null) {
  if (role !== null) {
    localStorage.setItem("access_token", makeJwt({ sub: "1", email: "test@example.com", role }));
  } else {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
  }
  return function Wrapper({ children }: { children: ReactNode }) {
    return <AuthProvider>{children}</AuthProvider>;
  };
}

afterEach(() => {
  localStorage.clear();
});

describe("useAuth role helpers", () => {
  it("isAdmin is true when user.role is 'admin'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("admin") });
    expect(result.current.isAdmin).toBe(true);
  });

  it("isAdmin is true when user.role is 'superadmin'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("superadmin") });
    expect(result.current.isAdmin).toBe(true);
  });

  it("isAdmin is false when user.role is 'user'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("user") });
    expect(result.current.isAdmin).toBe(false);
  });

  it("isSuperadmin is true only when user.role is 'superadmin'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("superadmin") });
    expect(result.current.isSuperadmin).toBe(true);
  });

  it("isSuperadmin is false when user.role is 'admin'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("admin") });
    expect(result.current.isSuperadmin).toBe(false);
  });

  it("isUser is true only when user.role is 'user'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("user") });
    expect(result.current.isUser).toBe(true);
  });

  it("isUser is false when user.role is 'admin'", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole("admin") });
    expect(result.current.isUser).toBe(false);
  });

  it("all three helpers are false when user is null (unauthenticated)", () => {
    const { result } = renderHook(() => useAuth(), { wrapper: wrapperWithRole(null) });
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isSuperadmin).toBe(false);
    expect(result.current.isUser).toBe(false);
  });
});
