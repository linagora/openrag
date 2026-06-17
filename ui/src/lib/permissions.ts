import { useAuth } from "./auth";
import type { PartitionRole } from "./api/partitions";

// Central capability layer. Pages ask `usePermissions()` what the caller can do
// rather than reading `is_admin` / roles directly — so when the SaaS org layer
// (platform-admin > org-admin > partition role) and the OIDC auth rework land,
// only this file changes, not every page.
//
// Client gating is UX only; the server enforces every permission. Today the only
// real scopes are global `is_admin` and per-partition role; org scoping is added
// here when the orgs backend exists (the platform capabilities below become
// org-scoped, partition capabilities are unchanged).

const WRITE_ROLES = new Set<string>(["editor", "owner"]);

type Role = PartitionRole | string | null | undefined;

export interface Permissions {
  isAdmin: boolean;
  // Platform-scoped (today: global admin). Models/Presets/Users/Partitions become
  // org-admin too under SaaS; System/Platform stay platform-operator only.
  canViewPlatform: boolean;
  canViewSystem: boolean;
  canManageUsers: boolean;
  canManageModels: boolean;
  canManagePresets: boolean;
  canManagePartitions: boolean;
  // Partition-scoped — pass the caller's role on the partition in question.
  canRead: (role: Role) => boolean;
  canWrite: (role: Role) => boolean;
  canManageMembers: (role: Role) => boolean;
}

export function usePermissions(): Permissions {
  const { isAdmin } = useAuth();
  return {
    isAdmin,
    canViewPlatform: isAdmin,
    canViewSystem: isAdmin,
    canManageUsers: isAdmin,
    canManageModels: isAdmin,
    canManagePresets: isAdmin,
    canManagePartitions: isAdmin,
    canRead: (role) => isAdmin || !!role,
    canWrite: (role) => isAdmin || (role ? WRITE_ROLES.has(role) : false),
    canManageMembers: (role) => isAdmin || role === "owner",
  };
}
