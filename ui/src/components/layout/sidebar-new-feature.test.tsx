import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "./sidebar";

vi.mock("@/lib/api/system", () => ({
  getVersion: vi.fn().mockResolvedValue({ version: "2.2.0" }),
}));

vi.mock("@/lib/jobs-queries", () => ({
  useActiveJobsCount: () => 0,
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: true,
    canViewSystem: true,
    canManageUsers: true,
    canManageModels: true,
    canManagePresets: true,
    canManagePrompts: true,
  }),
}));

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

describe("AppSidebar new feature markers", () => {
  it("marks Jobs as new without changing the link name", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SidebarProvider>
            <AppSidebar />
          </SidebarProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const jobsLink = screen.getByRole("link", { name: "Jobs" });
    const marker = within(jobsLink).getByText("NEW");
    expect(marker.className).toContain("group-data-[collapsible=icon]:hidden");
  });
});
