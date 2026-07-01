import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import OverviewPage from "./overview";

const queryData = vi.hoisted(() => ({
  partitions: [
    {
      partition: "shared-docs",
      name: "shared-docs",
      role: "viewer",
      document_count: 3,
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = queryKey[0];
    if (key === "partitions") {
      return {
        data: { partitions: queryData.partitions },
        isLoading: false,
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
    canCreatePartition: true,
    canWrite: (role: string | null | undefined) => role === "editor" || role === "owner",
  }),
}));

describe("OverviewPage quick actions", () => {
  beforeEach(() => {
    queryData.partitions = [
      {
        partition: "shared-docs",
        name: "shared-docs",
        role: "viewer",
        document_count: 3,
        created_at: "2026-01-01T00:00:00Z",
      },
    ];
  });

  it("guides users without a writable partition to create one before upload", () => {
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Upload Documents")).toBeNull();

    const action = screen.getByRole("link", { name: /create partition to upload/i });
    expect(action.getAttribute("href")).toBe("/partitions?create=1");
    expect(screen.getByText("Create a partition first, then add documents")).toBeTruthy();
  });

  it("keeps upload available when the user has a writable partition", () => {
    queryData.partitions = [
      {
        partition: "shared-docs",
        name: "shared-docs",
        role: "viewer",
        document_count: 3,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        partition: "team-upload",
        name: "team-upload",
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
});
