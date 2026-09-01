import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "./sidebar";

const activeJobs = vi.hoisted(() => ({ count: 0 }));

vi.mock("@/lib/api/system", () => ({
  getVersion: vi.fn().mockResolvedValue({ version: "2.2.0" }),
}));

vi.mock("@/lib/jobs-queries", () => ({
  useActiveJobsCount: () => activeJobs.count,
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

function renderSidebar() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SidebarProvider>
          <AppSidebar />
        </SidebarProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppSidebar new feature markers", () => {
  beforeEach(() => {
    activeJobs.count = 0;
  });

  it("marks Jobs as new without changing the link name", () => {
    renderSidebar();

    const jobsLink = screen.getByRole("link", { name: "Jobs" });
    const marker = within(jobsLink).getByText("NEW");
    expect(marker.className).toContain("group-data-[collapsible=icon]:hidden");
  });

  it("keeps the NEW marker and active-job count independent", () => {
    activeJobs.count = 7;
    renderSidebar();

    const jobsLink = screen.getByRole("link", { name: "Jobs, 7 active jobs" });
    expect(within(jobsLink).getByText("NEW")).toBeTruthy();
    expect(jobsLink.querySelector('[data-slot="jobs-badge"]')?.textContent).toBe("7");
  });
});
