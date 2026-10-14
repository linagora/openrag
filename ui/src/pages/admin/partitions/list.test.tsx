import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deletePartition, listPartitions } from "@/lib/api/partitions";
import { listModelEndpoints } from "@/lib/api/models";
import PartitionListPage from "./list";

const permissions = vi.hoisted(() => ({
  canManagePartitions: false,
  canConfigurePartition: vi.fn((role?: string) => role === "owner"),
  canWrite: vi.fn((role?: string) => role === "owner" || role === "editor"),
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
const listModelEndpointsMock = vi.mocked(listModelEndpoints);

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

function renderPartitions(cachedPartitions?: ReturnType<typeof makePartition>[]) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  if (cachedPartitions) queryClient.setQueryData(["partitions"], { partitions: cachedPartitions });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PartitionListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PartitionListPage read errors", () => {
  beforeEach(() => {
    listPartitionsMock.mockReset();
  });

  it("shows a retryable load error instead of an empty state and recovers on retry", async () => {
    listPartitionsMock
      .mockRejectedValueOnce(new Error("service unavailable"))
      .mockResolvedValueOnce({ partitions: [makePartition("recovered")] });
    renderPartitions();

    expect(await screen.findByText("Partitions could not be loaded")).not.toBeNull();
    expect(screen.queryByText("No partitions.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("recovered")).not.toBeNull();
  });

  it("keeps cached partitions visible and labels them stale after refresh fails", async () => {
    listPartitionsMock.mockResolvedValueOnce({ partitions: [makePartition("cached-partition")] });
    renderPartitions([makePartition("cached-partition")]);

    expect(await screen.findByText("cached-partition")).not.toBeNull();
    await waitFor(() => expect(listPartitionsMock).toHaveBeenCalledOnce());
    listPartitionsMock.mockRejectedValueOnce(new Error("service unavailable"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh partitions" }));
    expect(await screen.findByText(/Showing previously loaded partitions/)).not.toBeNull();
    expect(screen.getByText("cached-partition")).not.toBeNull();
    expect(screen.queryByText("No partitions.")).toBeNull();
  });

  it("shows and retries failures while loading partition setup options", async () => {
    permissions.canManagePartitions = true;
    listModelEndpointsMock.mockReset().mockRejectedValueOnce(new Error("service unavailable"));
    const user = userEvent.setup();
    renderPartitions();

    await user.click(screen.getByRole("button", { name: "Create Partition" }));
    expect(await screen.findByText("Partition setup options could not be loaded")).not.toBeNull();
    listModelEndpointsMock.mockResolvedValue([]);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Embedder *")).not.toBeNull();
    expect(screen.queryByText("Partition setup options could not be loaded")).toBeNull();
  });
});

describe("PartitionListPage bulk actions", () => {
  beforeEach(() => {
    permissions.canManagePartitions = false;
    permissions.canConfigurePartition.mockImplementation((role?: string) => role === "owner");
    permissions.canWrite.mockImplementation((role?: string) => role === "owner" || role === "editor");
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

    const upload = await screen.findByRole("link", { name: /upload documents to owned-a/i });
    const edit = await screen.findByRole("link", { name: /edit owned-a/i });
    const deleteAction = screen.getByRole("button", { name: /delete owned-a/i });

    expect(upload.getAttribute("href")).toBe("/documents?partition=owned-a&upload=1");
    expect(upload.getAttribute("data-size")).toBe("icon-xs");
    expect(edit.getAttribute("data-size")).toBe("icon-xs");
    expect(deleteAction.getAttribute("data-size")).toBe("icon-xs");
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

  it("shows the upload action for editor-only partitions", async () => {
    listPartitionsMock.mockResolvedValue({
      partitions: [makePartition("editable-docs", "editor")],
    });

    renderPartitions();

    expect(await screen.findByRole("link", { name: /upload documents to editable-docs/i })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /delete editable-docs/i })).toBeNull();
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
