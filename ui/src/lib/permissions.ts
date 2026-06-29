import { useQuery } from "@tanstack/react-query";
import { useAuth } from "./auth";
import { getConfig } from "./api/system";
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
  // The backend only lets an admin bypass partition-membership checks when
  // SUPER_ADMIN_MODE is on (otherwise admins need a real role like anyone else).
  // `superAdmin` mirrors that so the UI never offers a partition action the API
  // will reject. TODO(org): becomes "platform super-admin OR org-admin of the
  // resource's org" once orgs exist.
  superAdmin: boolean;
  // Platform-scoped (today: global admin). Models/Presets/Users/Partitions become
  // org-admin too under SaaS; System/Platform stay platform-operator only.
  canViewPlatform: boolean;
  canViewSystem: boolean;
  canManageUsers: boolean;
  canManageModels: boolean;
  canManagePresets: boolean;
  canManagePartitions: boolean;
  // Any authenticated user may create (and thereby own) a partition — the backend's
  // POST /partition/{name} has no admin guard. Distinct from canManagePartitions,
  // which gates admin-only config of *all* partitions.
  canCreatePartition: boolean;
  // Partition-scoped — pass the caller's role on the partition in question.
  canRead: (role: Role) => boolean;
  canWrite: (role: Role) => boolean;
  canManageMembers: (role: Role) => boolean;
  // Edit a partition's own settings (description, presets, chat LLM). Backend gate
  // is require_partition_owner, so an owner (or a super-admin) only.
  canConfigurePartition: (role: Role) => boolean;
}

export function usePermissions(): Permissions {
  const { isAdmin } = useAuth();
  // SUPER_ADMIN_MODE governs the backend's admin→all-partitions bypass. /config is
  // admin-only and only admins need the flag (a non-admin never hits the bypass
  // branch), so the probe is admin-gated; it never changes at runtime.
  const { data: config } = useQuery({
    queryKey: ["system-config"],
    queryFn: getConfig,
    enabled: isAdmin,
    staleTime: Infinity,
  });
  const superAdmin = isAdmin && config?.super_admin_mode === true;

  return {
    isAdmin,
    superAdmin,
    canViewPlatform: isAdmin,
    canViewSystem: isAdmin,
    canManageUsers: isAdmin,
    canManageModels: isAdmin,
    canManagePresets: isAdmin,
    canManagePartitions: isAdmin,
    canCreatePartition: true,
    canRead: (role) => superAdmin || !!role,
    canWrite: (role) => superAdmin || (role ? WRITE_ROLES.has(role) : false),
    canManageMembers: (role) => superAdmin || role === "owner",
    canConfigurePartition: (role) => superAdmin || role === "owner",
  };
}
