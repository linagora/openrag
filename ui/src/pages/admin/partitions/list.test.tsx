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
    deletePartitionMock.mockResolvedValue();
    listPartitionsMock.mockResolvedValue({
      partitions: [
        {
          partition: "owned-a",
          name: "owned-a",
          role: "owner",
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
        },
        {
          partition: "viewer-b",
          name: "viewer-b",
          role: "viewer",
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
        },
      ],
    });
  });

  it("selects all deletable partitions and deletes only those partitions", async () => {
    renderPartitions();

    expect(await screen.findByText("owned-a")).not.toBeNull();
    expect(screen.getByText("viewer-b")).not.toBeNull();
    expect(screen.queryByText(/selected/i)).toBeNull();

    await userEvent.click(screen.getByRole("checkbox", { name: /select all deletable partitions/i }));

    expect(screen.getByText("1 selected")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /delete selected partitions/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(deletePartitionMock).toHaveBeenCalledWith("owned-a"));
    expect(deletePartitionMock).not.toHaveBeenCalledWith("viewer-b");
  });
});
