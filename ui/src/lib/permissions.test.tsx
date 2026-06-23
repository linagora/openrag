import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// usePermissions reads only `isAdmin` from useAuth — mock it (vi.hoisted so the
// factory may reference the shared state) instead of standing up AuthProvider.
const auth = vi.hoisted(() => ({ isAdmin: false }));
vi.mock("./auth", () => ({ useAuth: () => ({ isAdmin: auth.isAdmin }) }));

import { usePermissions } from "./permissions";

function perms(isAdmin: boolean) {
  auth.isAdmin = isAdmin;
  return renderHook(() => usePermissions()).result.current;
}

describe("usePermissions — platform scopes", () => {
  it("grants every platform capability to admins", () => {
    const p = perms(true);
    expect(p.isAdmin).toBe(true);
    expect(p.canViewPlatform).toBe(true);
    expect(p.canViewSystem).toBe(true);
    expect(p.canManageUsers).toBe(true);
    expect(p.canManageModels).toBe(true);
    expect(p.canManagePresets).toBe(true);
    expect(p.canManagePartitions).toBe(true);
  });

  it("denies platform capabilities to non-admins", () => {
    const p = perms(false);
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

  it("canManageMembers requires owner", () => {
    expect(perms(false).canManageMembers("viewer")).toBe(false);
    expect(perms(false).canManageMembers("editor")).toBe(false);
    expect(perms(false).canManageMembers("owner")).toBe(true);
  });
});

describe("usePermissions — admin overrides every partition-role check", () => {
  it("admin can read/write/manage regardless of (or absent) role", () => {
    const p = perms(true);
    expect(p.canRead(null)).toBe(true);
    expect(p.canWrite(null)).toBe(true);
    expect(p.canManageMembers(null)).toBe(true);
  });
});
