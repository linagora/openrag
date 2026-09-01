import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "./sidebar";

const activeJobs = vi.hoisted(() => ({ count: 7 }));
const permissions = vi.hoisted(() => ({
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
}));

vi.mock("@/lib/jobs-queries", () => ({
  useActiveJobsCount: () => activeJobs.count,
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => permissions,
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

const jobsBadge = () => screen.getByTitle("Jobs").querySelector('[data-slot="jobs-badge"]');

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
    permissions.isAdmin = true;
  });

  it.each([
    [1, "1", "Jobs, 1 active job"],
    [9, "9", "Jobs, 9 active jobs"],
    [99, "99", "Jobs, 99 active jobs"],
    [100, "99+", "Jobs, 100 active jobs"],
  ])("shows %i as %s and announces the exact total", (count, shown, name) => {
    activeJobs.count = count;
    renderSidebar();

    expect(jobsBadge()?.textContent).toBe(shown);
    expect(screen.getByRole("link", { name })).toBe(screen.getByTitle("Jobs"));
  });

  it("shows no badge at zero and leaves the active dot in place", () => {
    activeJobs.count = 0;
    renderSidebar("/jobs");

    const jobsLink = screen.getByTitle("Jobs");
    expect(jobsBadge()).toBeNull();
    expect(jobsLink.className).toContain("bg-sidebar-accent");
    expect(jobsLink.querySelector(".bg-sidebar-primary")).not.toBeNull();
    expect(screen.getByRole("link", { name: "Jobs" })).toBe(jobsLink);
  });

  it("hands the right edge to the badge while it is showing", () => {
    renderSidebar("/jobs");

    const jobsLink = screen.getByTitle("Jobs");
    expect(jobsBadge()).not.toBeNull();
    expect(jobsLink.querySelector(".bg-sidebar-primary")).toBeNull();
    expect(screen.getByTitle("Partitions").querySelector(".bg-sidebar-primary")).toBeNull();
  });

  it("leaves other navigation indicators unchanged", () => {
    renderSidebar("/partitions");

    expect(screen.getByTitle("Partitions").querySelector(".bg-sidebar-primary")).not.toBeNull();
    expect(screen.getByTitle("Partitions").querySelector('[data-slot="jobs-badge"]')).toBeNull();
  });

  it("keeps the badge visible and the Jobs link clickable when collapsed", async () => {
    const user = userEvent.setup();
    renderSidebar("/", false);

    const jobsLink = screen.getByRole("link", { name: "Jobs, 7 active jobs" });
    expect(jobsBadge()?.className).not.toContain("group-data-[collapsible=icon]:hidden");

    await user.click(jobsLink);
    expect(screen.getByLabelText("Current route").textContent).toBe("/jobs");
  });
});

describe("AppSidebar", () => {
  beforeEach(() => {
    permissions.isAdmin = true;
  });

  it("keeps the compact Release Notes button above the Settings separator", () => {
    renderSidebar();

    const releaseNotes = screen.getByRole("button", { name: /open release notes/i });
    const settings = screen.getByRole("link", { name: "Settings" });

    expect(releaseNotes.querySelector("svg")).toBeNull();
    expect(releaseNotes.className).toContain("h-7");
    expect(releaseNotes.closest("[data-slot='sidebar-release-notes']")?.className).toContain("group-data-[collapsible=icon]:hidden");
    expect(releaseNotes.closest("[data-slot='sidebar-release-notes']")?.nextElementSibling).toBe(
      screen.getByTestId("sidebar-footer"),
    );
    expect(screen.getByTestId("sidebar-content").compareDocumentPosition(releaseNotes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(settings.getAttribute("href")).toBe("/settings");
  });

  it("hides Release Notes from non-admin users", () => {
    permissions.isAdmin = false;
    renderSidebar();

    expect(screen.queryByRole("button", { name: /open release notes/i })).toBeNull();
  });
});
