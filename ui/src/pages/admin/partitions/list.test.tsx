import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PartitionListPage from "./list";

const permissions = vi.hoisted(() => ({
  canManagePartitions: false,
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

describe("PartitionListPage create partition copy", () => {
  beforeEach(() => {
    permissions.canManagePartitions = false;
  });

  it("describes create partition as a personal self-service action for normal users", async () => {
    renderPartitions();

    expect(screen.getByRole("button", { name: /create personal partition/i })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /create personal partition/i }));

    expect(screen.getByRole("heading", { name: /create personal partition/i })).not.toBeNull();
    expect(
      screen.getByText(
        "Create a personal document partition. You will be the owner and can upload documents to it.",
      ),
    ).not.toBeNull();
  });

  it("keeps the admin create dialog wording general", async () => {
    permissions.canManagePartitions = true;

    renderPartitions();

    expect(screen.getByRole("button", { name: /^create partition$/i })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /^create partition$/i }));

    expect(screen.getByRole("heading", { name: /^create partition$/i })).not.toBeNull();
    expect(screen.getByText("Create a new document partition with its configuration.")).not.toBeNull();
    expect(screen.queryByText("Create Personal Partition")).toBeNull();
  });
});
