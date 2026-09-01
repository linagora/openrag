import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "./sidebar";

const activeJobs = vi.hoisted(() => ({
  count: 7,
  isInitialLoading: false,
  hasResolvedOnce: true,
  isError: false,
}));

vi.mock("@/lib/jobs-queries", () => ({
  useActiveJobsCount: () => activeJobs,
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: true,
    superAdmin: true,
    superAdminModeResolved: true,
    canViewPlatform: true,
    canViewSystem: true,
    canManageUsers: true,
    canManageModels: true,
    canManagePresets: true,
    canManagePrompts: true,
    canManagePartitions: true,
    canCreatePartition: true,
    canRead: () => true,
    canWrite: () => true,
    canManageMembers: () => true,
    canConfigurePartition: () => true,
  }),
}));

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderSidebar(initialEntry = "/", defaultOpen = true) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SidebarProvider defaultOpen={defaultOpen}>
        <AppSidebar />
        <LocationProbe />
      </SidebarProvider>
    </MemoryRouter>,
  );
}

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

describe("AppSidebar Jobs badge", () => {
  beforeEach(() => {
    activeJobs.count = 7;
    activeJobs.isInitialLoading = false;
    activeJobs.hasResolvedOnce = true;
    activeJobs.isError = false;
  });

  it("shows the active count without changing Jobs navigation", async () => {
    const user = userEvent.setup();
    renderSidebar();

    const jobsLink = screen.getByTitle("Jobs");
    expect(screen.getByText("7")).not.toBeNull();

    await user.click(jobsLink);

    expect(screen.getByLabelText("Current route").textContent).toBe("/jobs");
    expect(jobsLink.className).toContain("bg-sidebar-accent");
  });

  it("replaces only the Jobs active dot and leaves other navigation indicators unchanged", () => {
    activeJobs.count = 0;
    const firstRender = renderSidebar("/jobs");

    const jobsLink = screen.getByTitle("Jobs");
    expect(jobsLink.className).toContain("bg-sidebar-accent");
    expect(jobsLink.querySelector(".bg-sidebar-primary")).toBeNull();

    firstRender.unmount();
    renderSidebar("/partitions");

    expect(screen.getByTitle("Partitions").querySelector(".bg-sidebar-primary")).not.toBeNull();
  });

  it("keeps the badge accessible and the Jobs link clickable when collapsed", async () => {
    const user = userEvent.setup();
    renderSidebar("/", false);

    const jobsLink = screen.getByRole("link", { name: "Jobs, 7 active jobs" });
    const badge = jobsLink.querySelector('[data-slot="jobs-badge"]');

    expect(badge?.className).not.toContain("group-data-[collapsible=icon]:hidden");
    await user.click(jobsLink);
    expect(screen.getByLabelText("Current route").textContent).toBe("/jobs");
  });
});
