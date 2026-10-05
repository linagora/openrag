import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { deletePreset, getPresetOptions, listPresets, updatePreset } from "@/lib/api/presets";
import type { PresetResponse } from "@/lib/api/presets";
import { listAllPrompts } from "@/lib/api/prompts";
import { listModelEndpoints, pickDefaultEndpoint } from "@/lib/api/models";
import { listPartitions } from "@/lib/api/partitions";
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

vi.mock("@/lib/api/models", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/models")>("@/lib/api/models");
  return {
    ...actual,
    listModelEndpoints: vi.fn().mockResolvedValue([]),
    pickDefaultEndpoint: vi.fn().mockReturnValue(undefined),
  };
});

vi.mock("@/lib/api/partitions", () => ({
  listPartitions: vi.fn().mockResolvedValue({ partitions: [] }),
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

describe("PresetsPage top_n change asks to check the LLM's context size", () => {
  const llm = (name: string, over: Record<string, unknown> = {}) => ({
    name,
    model_type: "llm" as const,
    endpoint: `http://${name}:8000/v1`,
    model_name: name,
    batch_size: 32,
    timeout: 60,
    extra: {},
    is_default: false,
    detected_max_llm_context_size: null,
    context_size_detection_pending: false,
    default_max_llm_context_size: 8192,
    default_max_output_tokens: 1024,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  });
  const partition = (name: string, retrieval_preset: string, chat_llm: string | null) => ({
    name,
    partition: name,
    retrieval_preset,
    chat_llm,
  });
  const retrievalPreset = makePreset({
    name: "default",
    preset_type: "retrieval",
    config: { type: "single", top_k: 50, top_n: 10 },
    used_by_partitions: 3,
  });

  beforeEach(() => {
    listPresetsMock.mockReset().mockResolvedValue([retrievalPreset]);
    updatePresetMock.mockReset().mockResolvedValue(retrievalPreset);
    listAllPromptsMock.mockReset().mockResolvedValue([]);
    vi.mocked(getPresetOptions).mockResolvedValue({
      chunking_strategies: [],
      parsing_strategies: [],
      retrieval_types: ["single"],
      reranker_providers: [],
      default_top_n: 10,
    });
    listModelEndpointsMock.mockReset().mockResolvedValue([
      // The default LLM, behind a gateway that reports no window.
      llm("gateway-mistral", { is_default: true }),
      llm("vllm-qwen", { detected_max_llm_context_size: 131072 }),
    ] as never);
    vi.mocked(pickDefaultEndpoint).mockImplementation((eps) => eps?.find((e) => e.is_default));
    vi.mocked(listPartitions).mockResolvedValue({
      partitions: [
        partition("docs", "default", null),
        // Names an LLM since deleted: the default LLM answers, as on the backend.
        partition("notes", "default", "gone-llm"),
        partition("research", "default", "vllm-qwen"),
        partition("other", "hyde", "vllm-qwen"),
      ],
    } as never);
  });

  afterEach(() => {
    vi.mocked(pickDefaultEndpoint).mockReset().mockReturnValue(undefined);
  });

  function renderRoutedPage() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <PresetsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  async function openRetrievalPreset(user: ReturnType<typeof userEvent.setup>) {
    renderRoutedPage();
    await user.click(await screen.findByRole("tab", { name: /retrieval/i }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    return { dialog, topN: within(dialog).getByPlaceholderText("Default: 10") as HTMLInputElement };
  }

  it("lists the LLMs answering for the preset's partitions, with their context size", async () => {
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.type(topN, "15");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/up to 15 chunks to generate the final answer/)).toBeTruthy();
    const gateway = (await within(confirm).findByText("gateway-mistral")).closest("li") as HTMLElement;
    expect(within(gateway).getByText("8,192 (system default)")).toBeTruthy();
    expect(within(gateway).getByText("for docs, notes")).toBeTruthy();
    expect(within(gateway).getByText(/doesn.t report its window/)).toBeTruthy();
    const qwen = within(confirm).getByText("vllm-qwen").closest("li") as HTMLElement;
    expect(within(qwen).getByText("131,072 (detected)")).toBeTruthy();
    expect(within(qwen).getByText("for research")).toBeTruthy();
    expect(within(qwen).queryByText(/doesn.t report its window/)).toBeNull();
    // All three partitions on the preset are listed.
    expect(within(confirm).queryByText(/isn.t listed|aren.t listed/)).toBeNull();
    expect(updatePresetMock).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole("button", { name: "Update" }));

    await waitFor(() =>
      expect(updatePresetMock).toHaveBeenCalledWith("retrieval", "default", {
        config: { type: "single", top_k: 50, top_n: 15 },
      }),
    );
  });

  it("follows a detection that is still running when it opens", async () => {
    // Still detecting for the form's fetch and the pop-up's own on opening:
    // only polling gets the result.
    let fetches = 0;
    listModelEndpointsMock.mockReset().mockImplementation(async (modelType) => {
      if (modelType !== "llm") return [] as never;
      return (
        ++fetches <= 2
          ? [
              llm("gateway-mistral", { is_default: true, context_size_detection_pending: true }),
              llm("vllm-qwen", { context_size_detection_pending: true }),
            ]
          : [llm("gateway-mistral", { is_default: true }), llm("vllm-qwen", { detected_max_llm_context_size: 131072 })]
      ) as never;
    });
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.type(topN, "15");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    await waitFor(() => expect(within(confirm).getByText("131,072 (detected)")).toBeTruthy(), { timeout: 4000 });
    expect(within(confirm).queryByText("Detecting…")).toBeNull();
  });

  it("goes back to the form without saving", async () => {
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.type(topN, "15");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Back" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(updatePresetMock).not.toHaveBeenCalled();
  });

  it("names RERANKER_TOP_K when top_n is cleared back to the default", async () => {
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/up to RERANKER_TOP_K chunks \(currently 10\)/)).toBeTruthy();
  });

  it("says how many partitions on the preset the admin can't see", async () => {
    // Without SUPER_ADMIN_MODE the partition list holds only the admin's memberships.
    listPresetsMock.mockResolvedValue([{ ...retrievalPreset, used_by_partitions: 5 }]);
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.type(topN, "15");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(await within(confirm).findByText(/2 more partitions on this preset aren.t listed/)).toBeTruthy();
    expect(within(confirm).getByText("gateway-mistral")).toBeTruthy();
  });

  it("doesn't call a preset unused when none of its partitions is visible", async () => {
    vi.mocked(listPartitions).mockResolvedValue({ partitions: [partition("other", "hyde", null)] } as never);
    listPresetsMock.mockResolvedValue([{ ...retrievalPreset, used_by_partitions: 1 }]);
    const user = userEvent.setup();
    const { dialog, topN } = await openRetrievalPreset(user);
    await user.clear(topN);
    await user.type(topN, "15");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(await within(confirm).findByText(/1 more partition on this preset isn.t listed/)).toBeTruthy();
    expect(within(confirm).queryByText(/No partition uses this preset/)).toBeNull();
  });

  it("saves straight away when top_n is unchanged", async () => {
    const user = userEvent.setup();
    const { dialog } = await openRetrievalPreset(user);
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    await waitFor(() => expect(updatePresetMock).toHaveBeenCalled());
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});
