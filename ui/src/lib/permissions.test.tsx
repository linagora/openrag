import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// usePermissions reads `isAdmin` from useAuth and `super_admin_mode` from the
// /config query — mock both (vi.hoisted so the factories may reference the shared
// state) instead of standing up AuthProvider + a QueryClient.
const auth = vi.hoisted(() => ({ isAdmin: false }));
const cfg = vi.hoisted(() => ({ superAdminMode: false, loading: false, error: false }));
vi.mock("./auth", () => ({ useAuth: () => ({ isAdmin: auth.isAdmin }) }));
vi.mock("@tanstack/react-query", () => ({
  useQuery: () => ({
    data: cfg.loading || cfg.error ? undefined : { super_admin_mode: cfg.superAdminMode },
    isSuccess: !cfg.loading && !cfg.error,
    isError: cfg.error,
  }),
}));

import { usePermissions } from "./permissions";

function perms(isAdmin: boolean, superAdminMode = false, state: Partial<typeof cfg> = {}) {
  auth.isAdmin = isAdmin;
  cfg.superAdminMode = superAdminMode;
  cfg.loading = state.loading ?? false;
  cfg.error = state.error ?? false;
  return renderHook(() => usePermissions()).result.current;
}

describe("usePermissions — platform scopes", () => {
  it("grants every platform capability to admins", () => {
    const p = perms(true);
    expect(p.isAdmin).toBe(true);
    expect(p.superAdminModeResolved).toBe(true);
    expect(p.canViewPlatform).toBe(true);
    expect(p.canViewSystem).toBe(true);
    expect(p.canManageUsers).toBe(true);
    expect(p.canManageModels).toBe(true);
    expect(p.canManagePresets).toBe(true);
    expect(p.canManagePartitions).toBe(true);
  });

  it("denies platform capabilities to non-admins", () => {
    const p = perms(false);
    expect(p.superAdminModeResolved).toBe(true);
    expect(p.canViewSystem).toBe(false);
    expect(p.canManageUsers).toBe(false);
    expect(p.canManageModels).toBe(false);
    expect(p.canManagePresets).toBe(false);
    expect(p.canManagePartitions).toBe(false);
  });

  it("lets any authenticated user create a partition", () => {
    expect(perms(false).canCreatePartition).toBe(true);
    expect(perms(true).canCreatePartition).toBe(true);
  });
});

describe("usePermissions — partition-scoped roles (non-admin)", () => {
  it("canRead requires any role", () => {
    expect(perms(false).canRead("viewer")).toBe(true);
    expect(perms(false).canRead(null)).toBe(false);
    expect(perms(false).canRead(undefined)).toBe(false);
  });

  it("canWrite requires editor or owner", () => {
    expect(perms(false).canWrite("viewer")).toBe(false);
    expect(perms(false).canWrite("editor")).toBe(true);
    expect(perms(false).canWrite("owner")).toBe(true);
    expect(perms(false).canWrite(null)).toBe(false);
  });

  it("canManageMembers / canConfigurePartition require owner", () => {
    expect(perms(false).canManageMembers("viewer")).toBe(false);
    expect(perms(false).canManageMembers("editor")).toBe(false);
    expect(perms(false).canManageMembers("owner")).toBe(true);
    expect(perms(false).canConfigurePartition("editor")).toBe(false);
    expect(perms(false).canConfigurePartition("owner")).toBe(true);
  });
});

describe("usePermissions — admin bypass is gated on SUPER_ADMIN_MODE", () => {
  it("keeps the admin bypass unresolved while /config is loading", () => {
    const p = perms(true, false, { loading: true });
    expect(p.superAdmin).toBe(false);
    expect(p.superAdminModeResolved).toBe(false);
  });

  it("a plain admin (mode off) does NOT bypass partition-role checks", () => {
    const p = perms(true, false);
    expect(p.superAdmin).toBe(false);
    expect(p.canRead(null)).toBe(false);
    expect(p.canWrite(null)).toBe(false);
    expect(p.canManageMembers(null)).toBe(false);
    expect(p.canConfigurePartition(null)).toBe(false);
  });

  it("a super-admin (mode on) bypasses every partition-role check", () => {
    const p = perms(true, true);
    expect(p.superAdmin).toBe(true);
    expect(p.canRead(null)).toBe(true);
    expect(p.canWrite(null)).toBe(true);
    expect(p.canManageMembers(null)).toBe(true);
    expect(p.canConfigurePartition(null)).toBe(true);
  });

  it("a non-admin never becomes super-admin even if the flag is on", () => {
    expect(perms(false, true).superAdmin).toBe(false);
  });
});
