import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { deletePreset, listPresets, updatePreset } from "@/lib/api/presets";
import type { PresetResponse } from "@/lib/api/presets";
import { listAllPrompts } from "@/lib/api/prompts";
import { listModelEndpoints } from "@/lib/api/models";
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
      retrieval_types: [],
      reranker_providers: [],
    }),
  };
});

vi.mock("@/lib/api/prompts", () => ({
  listAllPrompts: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/models", () => ({
  listModelEndpoints: vi.fn().mockResolvedValue([]),
  pickDefaultEndpoint: vi.fn().mockReturnValue(undefined),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", ResizeObserverMock);

beforeAll(() => {
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.setPointerCapture) Element.prototype.setPointerCapture = () => {};
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

const listPresetsMock = vi.mocked(listPresets);
const deletePresetMock = vi.mocked(deletePreset);
const updatePresetMock = vi.mocked(updatePreset);
const listAllPromptsMock = vi.mocked(listAllPrompts);
const listModelEndpointsMock = vi.mocked(listModelEndpoints);

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
});

describe("PresetsPage parsing configuration", () => {
  beforeEach(() => {
    listPresetsMock.mockReset();
    updatePresetMock.mockReset();
    listAllPromptsMock.mockReset();
    listModelEndpointsMock.mockReset();
  });

  it("marks both STT parsing controls as new", async () => {
    listPresetsMock.mockResolvedValue([makePreset()]);
    listAllPromptsMock.mockResolvedValue([]);
    listModelEndpointsMock.mockResolvedValue([]);
    const user = userEvent.setup();

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    for (const label of ["STT endpoint", "Transcription prompt"]) {
      const labelNode = screen.getByText(label);
      expect(within(labelNode.parentElement!).getByText("NEW")).toBeTruthy();
    }
  });

  it("submits explicit STT selections and can clear both back to inherited defaults", async () => {
    listPresetsMock.mockResolvedValue([makePreset()]);
    updatePresetMock.mockResolvedValue(makePreset());
    listModelEndpointsMock.mockImplementation(async (modelType) =>
      modelType === "stt"
        ? [{
            name: "moss-stt",
            model_type: "stt",
            endpoint: "http://moss:8000/v1",
            model_name: "moss-transcribe-diarize",
            batch_size: 1,
            timeout: 900,
            extra: {},
            is_default: false,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }]
        : [],
    );
    listAllPromptsMock.mockResolvedValue([{
      id: "asr-meeting",
      prompt_type: "asr_transcription",
      name: "meeting-notes",
      content: "Keep speaker labels.",
      is_default: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      used_by: 0,
    }]);
    const user = userEvent.setup();

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const selectFor = (label: string) => {
      const labelNode = screen.getByText(label);
      const container = labelNode.parentElement?.querySelector("[role='combobox']")
        ? labelNode.parentElement
        : labelNode.parentElement?.parentElement;
      return within(container as HTMLElement).getByRole("combobox");
    };
    const choose = async (label: string, option: string) => {
      await user.click(selectFor(label));
      await user.click(await screen.findByRole("option", { name: option }));
    };

    await choose("STT endpoint", "moss-stt");
    await choose("Transcription prompt", "meeting-notes");
    await user.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() =>
      expect(updatePresetMock).toHaveBeenNthCalledWith(1, "indexation", "legal", {
        config: {
          stt: "moss-stt",
          asr_transcription_prompt_name: "meeting-notes",
        },
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await choose("STT endpoint", "moss-stt");
    await choose("Transcription prompt", "meeting-notes");
    await choose("STT endpoint", "Use default");
    await choose("Transcription prompt", "Use default");
    await user.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() =>
      expect(updatePresetMock).toHaveBeenNthCalledWith(2, "indexation", "legal", {
        config: {
          stt: null,
          asr_transcription_prompt_name: null,
        },
      }),
    );
  });
});
