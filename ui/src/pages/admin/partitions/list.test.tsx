import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deletePartition, listPartitions } from "@/lib/api/partitions";
import PartitionListPage from "./list";

const permissions = vi.hoisted(() => ({
  canManagePartitions: false,
  canConfigurePartition: vi.fn((role?: string) => role === "owner"),
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => permissions,
}));

vi.mock("@/lib/api/partitions", () => ({
  listPartitions: vi.fn().mockResolvedValue({ partitions: [] }),
  createPartition: vi.fn(),
  deletePartition: vi.fn(),
}));

vi.mock("@/lib/api/presets", () => ({
  listPresets: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/models", () => ({
  listModelEndpoints: vi.fn().mockResolvedValue([]),
  validateStoredModelEndpoint: vi.fn(),
  pickDefaultEndpoint: vi.fn().mockReturnValue(null),
  resolveEmbedderName: vi.fn().mockReturnValue("default"),
}));

const listPartitionsMock = vi.mocked(listPartitions);
const deletePartitionMock = vi.mocked(deletePartition);

function makePartition(name: string, role = "owner") {
  return {
    partition: name,
    name,
    role,
    exists: true,
    description: "",
    embedder: "",
    dimension: 0,
    collection_name: null,
    chat_history_depth: 0,
    chat_llm: null,
    document_count: 0,
    indexation_preset: "",
    retrieval_preset: "",
    indexation_pipeline: {},
    retrieval_pipeline: {},
    created_at: null,
  };
}

function renderPartitions() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PartitionListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PartitionListPage bulk actions", () => {
  beforeEach(() => {
    permissions.canManagePartitions = false;
    permissions.canConfigurePartition.mockImplementation((role?: string) => role === "owner");
    deletePartitionMock.mockClear();
    deletePartitionMock.mockResolvedValue();
    listPartitionsMock.mockResolvedValue({
      partitions: [
        makePartition("owned-a"),
        makePartition("viewer-b", "viewer"),
      ],
    });
  });

  it("keeps the create dialog actions reachable with a scrollable form body", async () => {
    renderPartitions();

    await userEvent.click(screen.getByRole("button", { name: /create personal partition/i }));

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("max-h-[calc(100vh-2rem)]");
    expect(dialog.className).toContain("overflow-hidden");

    const form = dialog.querySelector("form");
    expect(form?.className).toContain("min-h-0");
    expect(form?.firstElementChild?.className).toContain("overflow-y-auto");
    expect(screen.getByRole("button", { name: "Cancel" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Create" })).not.toBeNull();
  });

  it("labels row icon actions", async () => {
    permissions.canManagePartitions = true;
    renderPartitions();

    expect(await screen.findByRole("link", { name: /edit owned-a/i })).not.toBeNull();
    expect(screen.getByRole("button", { name: /delete owned-a/i })).not.toBeNull();
  });

  it("keeps long partition names constrained while exposing the full name", async () => {
    const longName = "partition-with-a-very-long-benchmark-name-that-should-not-stretch-the-table";
    listPartitionsMock.mockResolvedValue({
      partitions: [makePartition(longName)],
    });

    renderPartitions();

    const partitionLink = await screen.findByRole("link", { name: longName });
    expect(partitionLink.getAttribute("title")).toBe(longName);
    expect(partitionLink.className).toContain("truncate");
  });

  it("selects all deletable partitions and deletes only those partitions", async () => {
    renderPartitions();

    expect(await screen.findByText("owned-a")).not.toBeNull();
    expect(screen.getByText("viewer-b")).not.toBeNull();
    expect(screen.queryByText(/selected/i)).toBeNull();

    await userEvent.click(screen.getByRole("checkbox", { name: /select visible deletable partitions/i }));

    expect(screen.getByText("1 selected")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /delete selected partitions/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(deletePartitionMock).toHaveBeenCalledWith("owned-a"));
    expect(deletePartitionMock).not.toHaveBeenCalledWith("viewer-b");
  });

  it("paginates partitions with 10 rows per page", async () => {
    listPartitionsMock.mockResolvedValue({
      partitions: Array.from({ length: 11 }, (_, i) =>
        makePartition(`partition-${String(i + 1).padStart(2, "0")}`),
      ),
    });

    renderPartitions();

    expect(await screen.findByText("partition-01")).not.toBeNull();
    expect(screen.getByText("partition-10")).not.toBeNull();
    expect(screen.queryByText("partition-11")).toBeNull();
    expect(screen.getByText("Page 1 of 2")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /next page/i }));

    expect(await screen.findByText("partition-11")).not.toBeNull();
    expect(screen.queryByText("partition-01")).toBeNull();
    expect(screen.getByText("Page 2 of 2")).not.toBeNull();
  });

  it("selects only visible partitions from the table header", async () => {
    listPartitionsMock.mockResolvedValue({
      partitions: Array.from({ length: 11 }, (_, i) =>
        makePartition(`partition-${String(i + 1).padStart(2, "0")}`),
      ),
    });

    renderPartitions();

    expect(await screen.findByText("partition-01")).not.toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: /select visible deletable partitions/i }));

    expect(screen.getByText("10 selected")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /delete selected partitions/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(deletePartitionMock).toHaveBeenCalledWith("partition-01"));
    expect(deletePartitionMock).toHaveBeenCalledTimes(10);
    expect(deletePartitionMock).not.toHaveBeenCalledWith("partition-11");
  });
});
