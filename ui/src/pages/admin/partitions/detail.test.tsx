import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { getPartitionConfig, listPartitions, listPartitionMembers, type PartitionConfig } from "@/lib/api/partitions";
import { listPresets } from "@/lib/api/presets";
import { listModelEndpoints } from "@/lib/api/models";
import { listAllPrompts } from "@/lib/api/prompts";
import PartitionDetailPage from "./detail";

vi.mock("@/lib/api/partitions", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/partitions")>("@/lib/api/partitions");
  return {
    ...actual,
    getPartitionConfig: vi.fn(),
    listPartitions: vi.fn(),
    listPartitionMembers: vi.fn(),
  };
});
vi.mock("@/lib/api/presets", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/presets")>("@/lib/api/presets");
  return { ...actual, listPresets: vi.fn() };
});
vi.mock("@/lib/api/models", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/models")>("@/lib/api/models");
  return { ...actual, listModelEndpoints: vi.fn() };
});
vi.mock("@/lib/api/prompts", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/prompts")>("@/lib/api/prompts");
  return { ...actual, listAllPrompts: vi.fn() };
});
vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: true,
    superAdmin: false,
    superAdminModeResolved: true,
    canManageMembers: (role: string | null | undefined) => role === "owner",
    canConfigurePartition: (role: string | null | undefined) => role === "owner",
    canRead: (role: string | null | undefined) => !!role,
    canWrite: (role: string | null | undefined) => role === "editor" || role === "owner",
  }),
}));
vi.mock("./use-embedder-swap", () => ({
  useEmbedderSwap: () => ({ swap: null, running: false }),
}));

const getPartitionConfigMock = vi.mocked(getPartitionConfig);
const listPartitionsMock = vi.mocked(listPartitions);
const listPartitionMembersMock = vi.mocked(listPartitionMembers);
const listPresetsMock = vi.mocked(listPresets);
const listModelEndpointsMock = vi.mocked(listModelEndpoints);
const listAllPromptsMock = vi.mocked(listAllPrompts);

function makePartition(): PartitionConfig {
  return {
    name: "docs",
    description: "Cached partition description",
    dimension: 768,
    document_count: 0,
    indexation_preset: "default-indexation",
    retrieval_preset: "default-retrieval",
    embedder: "default-embedder",
    indexed_embedders: [],
    chat_llm: null,
    chat_history_depth: 4,
    generation_prompt_names: {},
  } as unknown as PartitionConfig;
}

function renderPartitionDetail(cachedPartition?: PartitionConfig) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (cachedPartition) queryClient.setQueryData(["partition", "docs"], cachedPartition, { updatedAt: 0 });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/partitions/docs"]}>
        <Routes><Route path="/partitions/:name" element={<PartitionDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

describe("PartitionDetailPage read errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getPartitionConfigMock.mockReset();
    listPartitionsMock.mockReset().mockResolvedValue({ partitions: [{ partition: "docs", role: "owner" }] } as never);
    listPartitionMembersMock.mockReset().mockResolvedValue({ members: [] } as never);
    listPresetsMock.mockReset().mockResolvedValue([]);
    listModelEndpointsMock.mockReset().mockResolvedValue([]);
    listAllPromptsMock.mockReset().mockResolvedValue([]);
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

  it("does not offer retry when the caller is forbidden from viewing a partition", async () => {
    getPartitionConfigMock.mockRejectedValue(new ApiError(403, { detail: "Forbidden" }));
    renderPartitionDetail();

    expect(await screen.findByText("Partition is not available to your role")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("does not label a failed permission lookup as read-only", async () => {
    getPartitionConfigMock.mockResolvedValue(makePartition());
    listPartitionsMock.mockRejectedValueOnce(new Error("service unavailable"));
    renderPartitionDetail();

    expect(await screen.findByText("Partition permissions could not be checked")).not.toBeNull();
    expect(screen.queryByText(/read-only access/)).toBeNull();
  });

  it("blocks partition settings saves when required picker options fail to load", async () => {
    getPartitionConfigMock.mockResolvedValue(makePartition());
    let endpointReads = 0;
    listModelEndpointsMock.mockImplementation(async () => {
      if (++endpointReads === 1) throw new Error("service unavailable");
      return [];
    });
    renderPartitionDetail();

    expect(await screen.findByText("Settings options could not be loaded")).not.toBeNull();
    expect((screen.getByRole("button", { name: "Save Changes" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the cached partition visible when a refresh fails", async () => {
    getPartitionConfigMock.mockRejectedValueOnce(new Error("service unavailable"));
    renderPartitionDetail(makePartition());

    expect(await screen.findByText("Partition could not be refreshed")).not.toBeNull();
    expect(screen.getAllByText("Cached partition description")).toHaveLength(2);
  });

  it("does not offer retry when loading partition members is forbidden", async () => {
    getPartitionConfigMock.mockResolvedValue(makePartition());
    listPartitionMembersMock.mockRejectedValueOnce(new ApiError(403, { detail: "Forbidden" }));
    const user = userEvent.setup();
    renderPartitionDetail();

    await user.click(await screen.findByRole("tab", { name: "Users" }));
    expect(await screen.findByText("Partition users are not available to your role")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
