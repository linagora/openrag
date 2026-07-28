import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { deletePreset, listPresets } from "@/lib/api/presets";
import type { PresetResponse } from "@/lib/api/presets";
import PresetsPage from "./presets";

vi.mock("@/lib/api/presets", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/presets")>("@/lib/api/presets");
  return {
    ...actual,
    listPresets: vi.fn(),
    createPreset: vi.fn(),
    updatePreset: vi.fn(),
    deletePreset: vi.fn(),
    getPresetOptions: vi.fn().mockResolvedValue({
      chunking_strategies: [],
      parsing_strategies: [],
      table_reconstruction_modes: ["disabled", "automatic"],
      retrieval_types: [],
      reranker_providers: [],
    }),
  };
});

vi.mock("@/lib/api/prompts", () => ({
  listPrompts: vi.fn().mockResolvedValue({ prompts: [] }),
}));

vi.mock("@/lib/api/models", () => ({
  listModelEndpoints: vi.fn().mockResolvedValue([]),
  pickDefaultEndpoint: vi.fn().mockReturnValue(undefined),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const listPresetsMock = vi.mocked(listPresets);
const deletePresetMock = vi.mocked(deletePreset);

function makePreset(overrides: Partial<PresetResponse> = {}): PresetResponse {
  return {
    name: "legal",
    preset_type: "indexation",
    config: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    used_by_partitions: 0,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PresetsPage />
    </QueryClientProvider>,
  );
}

describe("PresetsPage usage badge", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    listPresetsMock.mockReset();
    deletePresetMock.mockReset();
    vi.mocked(toast.error).mockClear();
  });

  it("renders 'unused' for a preset with no referencing partitions", async () => {
    listPresetsMock.mockResolvedValue([makePreset({ name: "legal", used_by_partitions: 0 })]);

    renderPage();

    expect(await screen.findByText("legal")).toBeTruthy();
    expect(screen.getByText("unused")).toBeTruthy();
  });

  it("renders the partition count when a preset is in use", async () => {
    listPresetsMock.mockResolvedValue([makePreset({ name: "legal", used_by_partitions: 3 })]);

    renderPage();

    expect(await screen.findByText("legal")).toBeTruthy();
    expect(screen.getByText("used by 3 partitions")).toBeTruthy();
  });

  it("singularizes the badge label for exactly one partition", async () => {
    listPresetsMock.mockResolvedValue([makePreset({ name: "legal", used_by_partitions: 1 })]);

    renderPage();

    expect(await screen.findByText("used by 1 partition")).toBeTruthy();
  });

  it("surfaces a 409 conflict message via toast when deleting an in-use preset", async () => {
    listPresetsMock.mockResolvedValue([makePreset({ name: "legal", used_by_partitions: 2 })]);
    deletePresetMock.mockRejectedValue(
      new ApiError(409, { detail: "[CONFLICT]: Preset 'legal' is used by 2 partition(s); reassign them before deleting." }),
    );

    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("legal")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /delete/i }));
    await user.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(deletePresetMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining("used by 2 partition(s); reassign them before deleting"),
      ),
    );
  });

  it("exposes conservative cross-page table reconstruction in indexation presets", async () => {
    listPresetsMock.mockResolvedValue([]);
    const user = userEvent.setup();

    renderPage();

    await user.click(await screen.findByRole("button", { name: /add preset/i }));
    expect(
      await screen.findByText("Cross-page table reconstruction"),
    ).toBeTruthy();
    expect(
      screen.getByText(/Uncertain cases keep the parser output/i),
    ).toBeTruthy();
  });
});
