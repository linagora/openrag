import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import OverviewPage from "./overview";

const queryData = vi.hoisted(() => ({
  partitions: [
    {
      partition: "shared-docs",
      name: "Shared docs display name",
      role: "viewer",
      document_count: 3,
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
  isError: false,
  isLoading: false,
}));
const permissions = vi.hoisted(() => ({
  canCreatePartition: true,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = queryKey[0];
    if (key === "partitions") {
      return {
        data: { partitions: queryData.partitions },
        isError: queryData.isError,
        isLoading: queryData.isLoading,
      };
    }
    if (key === "tasks") {
      return {
        data: { tasks: [] },
        isLoading: false,
      };
    }
    return {
      data: undefined,
      isLoading: false,
      isSuccess: false,
    };
  },
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    user: {
      id: 2,
      display_name: "Viewer",
      is_admin: false,
      file_quota: 10,
      file_count: 0,
    },
  }),
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    canManageUsers: false,
    canViewSystem: false,
    canCreatePartition: permissions.canCreatePartition,
    canWrite: (role: string | null | undefined) => role === "editor" || role === "owner",
  }),
}));

describe("OverviewPage quick actions", () => {
  beforeEach(() => {
    sessionStorage.clear();
    permissions.canCreatePartition = true;
    queryData.isError = false;
    queryData.isLoading = false;
    queryData.partitions = [
      {
        partition: "shared-docs",
        name: "Shared docs display name",
        role: "viewer",
        document_count: 3,
        created_at: "2026-01-01T00:00:00Z",
      },
    ];
  });

  it("keeps one upload action and explains when no writable partition exists", async () => {
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Create Partition to Upload")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /upload documents/i }));

    expect(await screen.findByRole("heading", { name: /create a partition before uploading/i })).toBeTruthy();
    expect(
      await screen.findByText("You don't have a partition you can upload to yet. Create one first?"),
    ).toBeTruthy();
    expect((await screen.findByRole("link", { name: /^create partition$/i })).getAttribute("href")).toBe(
      "/partitions?create=1",
    );
  });

  it("keeps upload available when the user has a writable partition", () => {
    queryData.partitions = [
      {
        partition: "shared-docs",
        name: "Shared docs display name",
        role: "viewer",
        document_count: 3,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        partition: "team-upload",
        name: "Team upload display name",
        role: "editor",
        document_count: 0,
        created_at: "2026-01-01T00:00:00Z",
      },
    ];

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    const action = screen.getByRole("link", { name: /upload documents/i });
    expect(action.getAttribute("href")).toBe("/documents?partition=team-upload");
    expect(screen.queryByText("Create Partition to Upload")).toBeNull();
  });

  it("prefers the remembered partition when it is writable", () => {
    sessionStorage.setItem("documents.partition", "remembered-upload");
    queryData.partitions = [
      {
        partition: "first-upload",
        name: "First writable display name",
        role: "editor",
        document_count: 0,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        partition: "remembered-upload",
        name: "Remembered writable display name",
        role: "owner",
        document_count: 0,
        created_at: "2026-01-01T00:00:00Z",
      },
    ];

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    const action = screen.getByRole("link", { name: /upload documents/i });
    expect(action.getAttribute("href")).toBe("/documents?partition=remembered-upload");
  });

  it("does not show a derived upload action while partitions fail to load", () => {
    queryData.isError = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByText("Create Partition to Upload")).toBeNull();
  });

  it("hides upload guidance when nothing is writable and partition creation is unavailable", () => {
    permissions.canCreatePartition = false;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByText("Create Partition to Upload")).toBeNull();
  });
});
