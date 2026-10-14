import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { getPartitionConfig } from "@/lib/api/partitions";
import PartitionDetailPage from "./detail";

vi.mock("@/lib/api/partitions", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/partitions")>("@/lib/api/partitions");
  return { ...actual, getPartitionConfig: vi.fn() };
});

const getPartitionConfigMock = vi.mocked(getPartitionConfig);

function renderPartitionDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/partitions/docs"]}>
        <Routes><Route path="/partitions/:name" element={<PartitionDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PartitionDetailPage read errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getPartitionConfigMock.mockReset();
  });

  it("offers retry for a service failure rather than reporting a missing partition", async () => {
    getPartitionConfigMock.mockRejectedValue(new Error("service unavailable"));
    renderPartitionDetail();

    expect(await screen.findByText("Partition could not be loaded")).not.toBeNull();
    expect(screen.queryByText("Partition not found.")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeNull();
  });

  it("reports not found only for a 404 response", async () => {
    getPartitionConfigMock.mockRejectedValue(new ApiError(404, { detail: "Not found" }));
    renderPartitionDetail();

    expect(await screen.findByText("Partition not found.")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
