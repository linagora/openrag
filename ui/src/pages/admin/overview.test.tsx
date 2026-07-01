import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import OverviewPage from "./overview";

type TestPartition = {
  partition: string;
  name: string;
  role: string;
  document_count: number;
  created_at: string;
};

const makePartition = vi.hoisted(
  () =>
    (overrides: Partial<TestPartition> = {}): TestPartition => ({
      partition: "shared-docs",
      name: "Shared docs display name",
      role: "viewer",
      document_count: 3,
      created_at: "2026-01-01T00:00:00Z",
      ...overrides,
    }),
);
const queryData = vi.hoisted(() => ({
  partitions: [makePartition()],
  isError: false,
  isLoading: false,
}));
const permissions = vi.hoisted(() => ({
  canCreatePartition: true,
  canManagePartitions: false,
}));
const authUser = vi.hoisted(() => ({
  is_admin: false,
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
      is_admin: authUser.is_admin,
      file_quota: 10,
      file_count: 0,
    },
  }),
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    canManageUsers: false,
    canViewSystem: false,
    canManagePartitions: permissions.canManagePartitions,
    canCreatePartition: permissions.canCreatePartition,
    canWrite: (role: string | null | undefined) => role === "editor" || role === "owner",
  }),
}));

describe("OverviewPage quick actions", () => {
  beforeEach(() => {
    sessionStorage.clear();
    permissions.canCreatePartition = true;
    permissions.canManagePartitions = false;
    authUser.is_admin = false;
    queryData.isError = false;
    queryData.isLoading = false;
    queryData.partitions = [makePartition()];
  });

  it("describes partition creation as self-service for normal users", () => {
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Create Personal Partition")).not.toBeNull();
    expect(screen.getByText("Create a personal partition for your documents")).not.toBeNull();
  });

  it("keeps the management create partition wording general", () => {
    permissions.canManagePartitions = true;
    authUser.is_admin = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Create Partition")).not.toBeNull();
    expect(screen.getByText("Set up a new document partition")).not.toBeNull();
    expect(screen.queryByText("Create Personal Partition")).toBeNull();
    expect(screen.queryByText("Your file quota")).toBeNull();
  });

  it("updates create partition wording when management permission resolves", () => {
    const { rerender } = render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Create Personal Partition")).not.toBeNull();

    permissions.canManagePartitions = true;
    authUser.is_admin = true;

    rerender(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Create Partition")).not.toBeNull();
    expect(screen.getByText("Set up a new document partition")).not.toBeNull();
    expect(screen.queryByText("Create Personal Partition")).toBeNull();
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
      makePartition({
        partition: "shared-docs",
        name: "Shared docs display name",
        role: "viewer",
      }),
      makePartition({
        partition: "team-upload",
        name: "Team upload display name",
        role: "editor",
        document_count: 0,
      }),
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
      makePartition({
        partition: "first-upload",
        name: "First writable display name",
        role: "editor",
        document_count: 0,
      }),
      makePartition({
        partition: "remembered-upload",
        name: "Remembered writable display name",
        role: "owner",
        document_count: 0,
      }),
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
