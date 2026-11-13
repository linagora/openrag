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
  isLoadingError: false,
  isRefetchError: false,
  tasksIsLoadingError: false,
  tasksIsError: false,
  tasksIsRefetchError: false,
  tasksIsLoading: false,
  refetch: vi.fn(),
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
        data: queryData.isLoadingError ? undefined : { partitions: queryData.partitions },
        isError: queryData.isError,
        isLoading: queryData.isLoading,
        isLoadingError: queryData.isLoadingError,
        isRefetchError: queryData.isRefetchError,
        isFetching: false,
        refetch: queryData.refetch,
      };
    }
    if (key === "tasks") {
      return {
        data: queryData.tasksIsLoadingError ? undefined : { tasks: [] },
        isLoading: queryData.tasksIsLoading,
        isError: queryData.tasksIsError,
        isLoadingError: queryData.tasksIsLoadingError,
        isRefetchError: queryData.tasksIsRefetchError,
        isFetching: false,
        refetch: queryData.refetch,
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
    queryData.isLoadingError = false;
    queryData.isRefetchError = false;
    queryData.tasksIsLoadingError = false;
    queryData.tasksIsError = false;
    queryData.tasksIsRefetchError = false;
    queryData.tasksIsLoading = false;
    queryData.refetch.mockClear();
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
    queryData.isLoadingError = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /upload documents/i })).toBeNull();
    expect(screen.queryByText("Create Partition to Upload")).toBeNull();
  });

  it("keeps derived actions based on cached partitions after a refresh failure", () => {
    queryData.isError = true;
    queryData.isRefetchError = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: /upload documents/i })).not.toBeNull();
    expect(screen.getAllByText(/previously loaded value/)).not.toHaveLength(0);
  });

  it("does not show a zero-job summary when the task request fails", () => {
    queryData.tasksIsLoadingError = true;
    queryData.tasksIsError = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Active Jobs unavailable")).not.toBeNull();
    expect(screen.getByText("Recent jobs could not be loaded")).not.toBeNull();
    expect(screen.queryByText("No jobs found.")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry" })).not.toHaveLength(0);
  });

  it("keeps the recent-job summary visible and labels it stale after refresh fails", () => {
    queryData.tasksIsRefetchError = true;

    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText(/Showing previously loaded jobs/)).not.toBeNull();
    expect(screen.queryByText("Recent jobs could not be loaded")).toBeNull();
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
