import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SuperAdminModeIndicator } from "./super-admin-mode-indicator";

const authState = vi.hoisted(() => ({ isAdmin: false }));
const configState = vi.hoisted(() => ({ superAdminMode: false }));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ isAdmin: authState.isAdmin }),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ enabled }: { enabled?: boolean }) => ({
    data: enabled ? { super_admin_mode: configState.superAdminMode } : undefined,
  }),
}));

describe("SuperAdminModeIndicator", () => {
  it("shows deployment-wide access context to admins when super admin mode is enabled", () => {
    authState.isAdmin = true;
    configState.superAdminMode = true;

    render(<SuperAdminModeIndicator />);

    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText("Super Admin Mode enabled for this deployment.")).toBeTruthy();
    expect(
      screen.getByText("Admins can access all partitions and documents in this deployment."),
    ).toBeTruthy();
  });

  it("is hidden for admins when super admin mode is disabled", () => {
    authState.isAdmin = true;
    configState.superAdminMode = false;

    render(<SuperAdminModeIndicator />);

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is hidden for normal users even if the deployment enables super admin mode", () => {
    authState.isAdmin = false;
    configState.superAdminMode = true;

    render(<SuperAdminModeIndicator />);

    expect(screen.queryByRole("status")).toBeNull();
  });
});
