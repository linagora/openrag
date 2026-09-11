import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createModelEndpoint,
  getModelEndpointIndexedUsage,
  listModelEndpoints,
  setDefaultModelEndpoint,
  updateModelEndpoint,
  validateModelEndpoint,
} from "@/lib/api/models";
import ModelsPage from "./models";

vi.mock("@/lib/api/models", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/models")>("@/lib/api/models");
  return {
    ...actual,
    listModelEndpoints: vi.fn(),
    createModelEndpoint: vi.fn(),
    updateModelEndpoint: vi.fn(),
    deleteModelEndpoint: vi.fn(),
    setDefaultModelEndpoint: vi.fn(),
    revealModelEndpointApiKey: vi.fn(),
    validateModelEndpoint: vi.fn(),
    getModelEndpointIndexedUsage: vi.fn(),
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

const listModelEndpointsMock = vi.mocked(listModelEndpoints);
const updateModelEndpointMock = vi.mocked(updateModelEndpoint);
const validateModelEndpointMock = vi.mocked(validateModelEndpoint);
const getIndexedUsageMock = vi.mocked(getModelEndpointIndexedUsage);
const setDefaultMock = vi.mocked(setDefaultModelEndpoint);
const createModelEndpointMock = vi.mocked(createModelEndpoint);

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false },
    setPointerCapture: { configurable: true, value: () => undefined },
    releasePointerCapture: { configurable: true, value: () => undefined },
    scrollIntoView: { configurable: true, value: () => undefined },
  });
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("ModelsPage validation", () => {
  beforeEach(() => {
    listModelEndpointsMock.mockReset().mockResolvedValue([]);
    updateModelEndpointMock.mockReset();
    validateModelEndpointMock.mockReset().mockResolvedValue({
      reachable: true,
      model_found: true,
      transcription_supported: true,
    });
  });

  it("sends the selected reranker implementation during draft validation", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "reranker" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://reranker:8000");
    await user.type(textboxes[2], "jina-reranker-v2");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(validateModelEndpointMock).toHaveBeenCalledWith(
        expect.objectContaining({
          model_type: "reranker",
          extra: { implementation: "infinity" },
        }),
      ),
    );
  });

  it("requires revalidation after changing the provider", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "reranker" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[0], "reranker");
    await user.type(textboxes[1], "http://reranker:8000");
    await user.type(textboxes[2], "jina-reranker-v2");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    const createButton = within(dialog).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    await waitFor(() => expect(createButton.disabled).toBe(false));
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(screen.getByRole("option", { name: /openai/i }));

    await waitFor(() => expect(createButton.disabled).toBe(true));
  });

  it("does not restore validation for an edited endpoint after its provider changes", async () => {
    listModelEndpointsMock.mockResolvedValue([
      {
        name: "reranker",
        model_type: "reranker",
        endpoint: "http://reranker:8000",
        model_name: "jina-reranker-v2",
        batch_size: 32,
        timeout: 30,
        extra: { implementation: "infinity" },
        has_api_key: false,
        is_default: true,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "reranker" }));
    await screen.findByText("jina-reranker-v2");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = screen.getByRole("dialog");
    const updateButton = within(dialog).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    await waitFor(() => expect(updateButton.disabled).toBe(false));
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(screen.getByRole("option", { name: /openai/i }));

    await waitFor(() => expect(updateButton.disabled).toBe(true));
  });

  it("persists the STT API key used by draft validation", async () => {
    listModelEndpointsMock.mockResolvedValue([
      {
        name: "moss",
        model_type: "stt",
        endpoint: "http://moss:8000/v1",
        model_name: "moss-transcribe-diarize",
        batch_size: 1,
        timeout: 3600,
        extra: { api_key: "sk-o********" },
        has_api_key: true,
        is_default: true,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await screen.findByText("moss-transcribe-diarize");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = screen.getByRole("dialog");
    const extraInput = within(dialog).getByDisplayValue("{}");
    fireEvent.change(extraInput, {
      target: { value: JSON.stringify({ api_key: "replacement-key" }) },
    });
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(validateModelEndpointMock).toHaveBeenCalledWith(
        expect.objectContaining({ extra: { api_key: "replacement-key" } }),
      ),
    );
    const updateButton = within(dialog).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    await waitFor(() => expect(updateButton.disabled).toBe(false));
    await user.click(updateButton);

    await waitFor(() =>
      expect(updateModelEndpointMock).toHaveBeenCalledWith(
        "stt",
        "moss",
        expect.objectContaining({ extra: { api_key: "replacement-key" } }),
      ),
    );
  });

  it("validates an STT draft with the timeout currently entered in the form", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.type(within(dialog).getByPlaceholderText("fr"), "fr");
    const extraInput = within(dialog).getByDisplayValue("{}");
    fireEvent.change(extraInput, {
      target: { value: JSON.stringify({ response_format: "json", temperature: 0 }) },
    });
    const numberInputs = within(dialog).getAllByRole("spinbutton");
    await user.clear(numberInputs[1]);
    await user.type(numberInputs[1], "725");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(validateModelEndpointMock).toHaveBeenCalledWith(
        expect.objectContaining({
          endpoint: "http://moss:8000/v1",
          model_type: "stt",
          model_name: "moss-transcribe-diarize",
          timeout: 725,
          extra: {
            language: "fr",
            response_format: "json",
            temperature: 0,
          },
        }),
      ),
    );
  });

  it("includes speaker-aware MOSS normalization in STT draft validation", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.click(
      within(dialog).getByRole("switch", {
        name: "Enable speaker-aware MOSS transcript normalization",
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(validateModelEndpointMock).toHaveBeenCalledWith(
        expect.objectContaining({ extra: { moss_speaker_aware: true } }),
      ),
    );
  });

  it("requires revalidation after changing an STT draft timeout", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    const createButton = within(dialog).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    await waitFor(() => expect(createButton.disabled).toBe(false));

    const timeoutInput = within(dialog).getAllByRole("spinbutton")[1];
    await user.clear(timeoutInput);
    await user.type(timeoutInput, "725");

    await waitFor(() => expect(createButton.disabled).toBe(true));
  });

  it("requires revalidation after changing a stored STT endpoint timeout", async () => {
    listModelEndpointsMock.mockResolvedValue([
      {
        name: "moss",
        model_type: "stt",
        endpoint: "http://moss:8000/v1",
        model_name: "moss-transcribe-diarize",
        batch_size: 1,
        timeout: 3600,
        extra: {},
        is_default: true,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await screen.findByText("moss-transcribe-diarize");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = screen.getByRole("dialog");
    const updateButton = within(dialog).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    await waitFor(() => expect(updateButton.disabled).toBe(false));

    const timeoutInput = within(dialog).getAllByRole("spinbutton")[1];
    await user.clear(timeoutInput);
    await user.type(timeoutInput, "725");

    await waitFor(() => expect(updateButton.disabled).toBe(true));
  });

  it("requires revalidation after changing an STT language hint", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    const createButton = within(dialog).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    await waitFor(() => expect(createButton.disabled).toBe(false));

    await user.type(within(dialog).getByPlaceholderText("fr"), "fr");

    await waitFor(() => expect(createButton.disabled).toBe(true));
  });

  it("requires revalidation after changing speaker-aware MOSS normalization", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    const createButton = within(dialog).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    await waitFor(() => expect(createButton.disabled).toBe(false));

    await user.click(
      within(dialog).getByRole("switch", {
        name: "Enable speaker-aware MOSS transcript normalization",
      }),
    );

    await waitFor(() => expect(createButton.disabled).toBe(true));
  });

  it("ignores a validation response for an outdated STT draft", async () => {
    let resolveValidation!: (value: {
      reachable: boolean;
      model_found: boolean;
      transcription_supported: boolean;
    }) => void;
    validateModelEndpointMock.mockReturnValue(
      new Promise((resolve) => {
        resolveValidation = resolve;
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await user.click(screen.getByRole("button", { name: /add endpoint/i }));

    const dialog = screen.getByRole("dialog");
    const textboxes = within(dialog).getAllByRole("textbox");
    await user.type(textboxes[1], "http://moss:8000/v1");
    await user.type(textboxes[2], "moss-transcribe-diarize");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalledOnce());

    const createButton = within(dialog).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    const timeoutInput = within(dialog).getAllByRole("spinbutton")[1];
    await user.clear(timeoutInput);
    await user.type(timeoutInput, "725");

    await act(async () => {
      resolveValidation({
        reachable: true,
        model_found: true,
        transcription_supported: true,
      });
    });

    expect(createButton.disabled).toBe(true);
    const validateButton = within(dialog).getByRole("button", { name: "Validate" }) as HTMLButtonElement;
    await waitFor(() => expect(validateButton.disabled).toBe(false));
  });

  it("ignores a validation response from a previously edited endpoint", async () => {
    listModelEndpointsMock.mockResolvedValue([
      {
        name: "moss-a",
        model_type: "stt",
        endpoint: "http://moss:8000/v1",
        model_name: "moss-transcribe-diarize",
        batch_size: 1,
        timeout: 3600,
        extra: { api_key: "sk-a********" },
        has_api_key: true,
        is_default: true,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
      {
        name: "moss-b",
        model_type: "stt",
        endpoint: "http://moss:8000/v1",
        model_name: "moss-transcribe-diarize",
        batch_size: 1,
        timeout: 3600,
        extra: { api_key: "sk-b********" },
        has_api_key: true,
        is_default: false,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    ]);

    let resolveFirst!: (value: {
      reachable: boolean;
      model_found?: boolean;
      transcription_supported?: boolean;
    }) => void;
    let resolveSecond!: (value: {
      reachable: boolean;
      detail?: string;
    }) => void;
    validateModelEndpointMock
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSecond = resolve;
        }),
      );

    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "stt" }));
    await screen.findByText("moss-a");

    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    let dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalledTimes(1));
    expect(validateModelEndpointMock).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ stored_api_key_name: "moss-a" }),
    );

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await user.click(screen.getAllByRole("button", { name: "Edit" })[1]);
    dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalledTimes(2));
    expect(validateModelEndpointMock).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ stored_api_key_name: "moss-b" }),
    );

    await act(async () => {
      resolveFirst({ reachable: true, model_found: true, transcription_supported: true });
    });
    const validateButton = within(dialog).getByRole("button", { name: "Validate" }) as HTMLButtonElement;
    expect(validateButton.disabled).toBe(true);
    expect(within(dialog).queryByText(/Reachable —/)).toBeNull();

    await act(async () => {
      resolveSecond({ reachable: false, detail: "Endpoint B rejected its stored credential." });
    });
    const updateButton = within(dialog).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    await waitFor(() => expect(updateButton.disabled).toBe(true));
    expect(within(dialog).getByText("Endpoint B rejected its stored credential.")).toBeTruthy();
  });
});

describe("ModelsPage delete warning (#762)", () => {
  const embedder = (used_by_partitions: number) => ({
    name: "jina",
    model_type: "embedder" as const,
    endpoint: "http://vllm:8000/v1",
    model_name: "jina-embeddings-v3",
    batch_size: 32,
    timeout: 60,
    extra: {},
    has_api_key: false,
    is_default: true,
    used_by_partitions,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
  });

  beforeEach(() => {
    listModelEndpointsMock.mockReset().mockResolvedValue([]);
  });

  it("shows the real partition count instead of a static warning", async () => {
    listModelEndpointsMock.mockResolvedValue([{ ...embedder(3), is_default: false }]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("used by 3 partitions");
    await user.click(screen.getByRole("button", { name: /Delete/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/is the embedder for 3 partitions/)).toBeTruthy();
    expect(within(dialog).getByText(/the server will refuse/)).toBeTruthy();
  });

  it("does not promise a refusal for the default embedder's empty followers", async () => {
    // Its count includes partitions on the `default` alias; the empty ones
    // follow the promoted endpoint, so only the indexed ones block.
    listModelEndpointsMock.mockResolvedValue([embedder(3)]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("used by 3 partitions");
    await user.click(screen.getByRole("button", { name: /Delete/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/is the default embedder for 3 partitions/)).toBeTruthy();
    expect(within(dialog).getByText(/already holds indexed files/)).toBeTruthy();
    expect(within(dialog).queryByText(/the server will refuse/)).toBeNull();
  });

  it("says an unused endpoint deletes cleanly, with no count badge", async () => {
    listModelEndpointsMock.mockResolvedValue([embedder(0)]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("jina-embeddings-v3");
    expect(screen.queryByText(/used by/)).toBeNull();
    await user.click(screen.getByRole("button", { name: /Delete/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/This will permanently delete "jina"/)).toBeTruthy();
  });

  it("tells the truth about an LLM: reset to default, not refused", async () => {
    listModelEndpointsMock.mockResolvedValue([
      { ...embedder(2), name: "mistral", model_type: "llm" as const, model_name: "mistral-small" },
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "llm" }));
    await screen.findByText("mistral-small");
    await user.click(screen.getByRole("button", { name: /Delete/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/resets them to the default LLM/)).toBeTruthy();
  });
});


describe("ModelsPage embedder edit guard (#762 C)", () => {
  const endpoint = (over: Record<string, unknown> = {}) => ({
    name: "qwen",
    model_type: "embedder" as const,
    endpoint: "https://a.example/v1",
    model_name: "Qwen3-Embedding-0.6B",
    batch_size: 32,
    timeout: 30,
    extra: {},
    has_api_key: false,
    is_default: true,
    used_by_partitions: 1,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...over,
  });

  beforeEach(() => {
    listModelEndpointsMock.mockReset().mockResolvedValue([endpoint()]);
    updateModelEndpointMock.mockReset().mockResolvedValue(endpoint() as never);
    validateModelEndpointMock.mockReset().mockResolvedValue({ reachable: true } as never);
    getIndexedUsageMock.mockReset().mockResolvedValue({
      partitions: [{ partition: "docs", file_count: 31 }],
      total_files: 31,
    } as never);
  });

  const openEditForm = async (user: ReturnType<typeof userEvent.setup>) => {
    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));
    const intro = await screen.findByRole("alertdialog");
    await user.click(within(intro).getByRole("button", { name: "Modify this one" }));
    return screen.findByRole("dialog");
  };

  it("explains the vector space before opening the edit form", async () => {
    // The alternative is only a real choice while it is still a choice — after
    // the form is open and the new model typed, it reads as an obstacle.
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));

    const intro = await screen.findByRole("alertdialog");
    expect(within(intro).getByText(/An embedder owns its vector space/)).toBeTruthy();
    expect(within(intro).getByRole("button", { name: "Create a new one" })).toBeTruthy();
    expect(within(intro).getByRole("button", { name: "Modify this one" })).toBeTruthy();
    // The form itself must not be open yet.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens an embedder no partition uses straight into the form", async () => {
    // Nothing is indexed through it, so there is no vector space to protect.
    listModelEndpointsMock.mockResolvedValue([endpoint({ used_by_partitions: 0 })]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));

    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("opens a non-embedder edit straight into the form", async () => {
    // Repointing an LLM changes future answers, never a stored vector.
    listModelEndpointsMock.mockResolvedValue([
      endpoint({ name: "mistral", model_type: "llm" as const, model_name: "mistral-small" }),
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "llm" }));
    await screen.findByText("mistral-small");
    await user.click(screen.getByRole("button", { name: /Edit/ }));

    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("prefills the create form from the endpoint, under a free name", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));
    const intro = await screen.findByRole("alertdialog");
    await user.click(within(intro).getByRole("button", { name: "Create a new one" }));

    const form = await screen.findByRole("dialog");
    expect(within(form).getByDisplayValue("qwen-v2")).toBeTruthy();
    expect(within(form).getByDisplayValue("https://a.example/v1")).toBeTruthy();
    // Creating, not updating — the whole point of the alternative.
    expect(within(form).getByRole("button", { name: "Create" })).toBeTruthy();
  });

  it("opens Add Endpoint empty after creating from a copy", async () => {
    // Closing on success bypasses onOpenChange, which is where the copy was dropped.
    createModelEndpointMock.mockReset().mockResolvedValue(endpoint({ name: "qwen-v2" }) as never);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));
    const intro = await screen.findByRole("alertdialog");
    await user.click(within(intro).getByRole("button", { name: "Create a new one" }));
    const form = await screen.findByRole("dialog");
    await within(form).findByDisplayValue("qwen-v2");
    await user.click(within(form).getByRole("button", { name: /Validate/ }));
    const create = within(form).getByRole("button", { name: "Create" }) as HTMLButtonElement;
    await waitFor(() => expect(create.disabled).toBe(false));
    await user.click(create);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click(screen.getByRole("button", { name: /Add Endpoint/ }));
    const blank = await screen.findByRole("dialog");
    expect(within(blank).queryByDisplayValue("qwen-v2")).toBeNull();
    expect(within(blank).queryByDisplayValue("https://a.example/v1")).toBeNull();
  });

  it("holds a model change behind an explicit acknowledgement", async () => {
    const user = userEvent.setup();
    renderPage();

    const form = await openEditForm(user);
    const modelInput = within(form).getByDisplayValue("Qwen3-Embedding-0.6B");
    await user.clear(modelInput);
    await user.type(modelInput, "bge-m3");
    await user.click(within(form).getByRole("button", { name: /Validate/ }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalled());
    await user.click(within(form).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/This changes the vector space/)).toBeTruthy();
    const update = within(confirm).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    expect(update.disabled).toBe(true);
    expect(updateModelEndpointMock).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole("checkbox"));
    await waitFor(() => expect(update.disabled).toBe(false));
    await user.click(update);

    await waitFor(() => expect(updateModelEndpointMock).toHaveBeenCalled());
  });

  it("names how many indexed files ride on the endpoint", async () => {
    // A number is what makes the warning land; boilerplate gets clicked through.
    const user = userEvent.setup();
    renderPage();

    const form = await openEditForm(user);
    const modelInput = within(form).getByDisplayValue("Qwen3-Embedding-0.6B");
    await user.clear(modelInput);
    await user.type(modelInput, "bge-m3");
    await user.click(within(form).getByRole("button", { name: /Validate/ }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalled());
    await user.click(within(form).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(await within(confirm).findByText(/31 indexed files were built with this endpoint/)).toBeTruthy();
    expect(within(confirm).getByText("docs")).toBeTruthy();
  });

  const openModelChangeConfirm = async (user: ReturnType<typeof userEvent.setup>) => {
    const form = await openEditForm(user);
    const modelInput = within(form).getByDisplayValue("Qwen3-Embedding-0.6B");
    await user.clear(modelInput);
    await user.type(modelInput, "bge-m3");
    await user.click(within(form).getByRole("button", { name: /Validate/ }));
    await waitFor(() => expect(validateModelEndpointMock).toHaveBeenCalled());
    await user.click(within(form).getByRole("button", { name: "Update" }));
    return screen.findByRole("alertdialog");
  };

  it("keeps a model change unconfirmable until the indexed count arrives", async () => {
    // Ticking the box before the number shows acknowledges nothing.
    let resolveUsage: (value: never) => void = () => undefined;
    getIndexedUsageMock.mockReturnValue(new Promise((resolve) => (resolveUsage = resolve)));
    const user = userEvent.setup();
    renderPage();

    const confirm = await openModelChangeConfirm(user);
    await user.click(within(confirm).getByRole("checkbox"));
    const update = within(confirm).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    expect(update.disabled).toBe(true);

    resolveUsage({ partitions: [{ partition: "docs", file_count: 31 }], total_files: 31 } as never);

    await within(confirm).findByText(/31 indexed files were built with this endpoint/);
    await waitFor(() => expect(update.disabled).toBe(false));
  });

  it("offers a retry when the indexed count cannot be read, and waits for it", async () => {
    getIndexedUsageMock.mockRejectedValueOnce(new Error("database unavailable"));
    const user = userEvent.setup();
    renderPage();

    const confirm = await openModelChangeConfirm(user);
    await within(confirm).findByText(/Could not check what is indexed with this endpoint/);
    await user.click(within(confirm).getByRole("checkbox"));
    const update = within(confirm).getByRole("button", { name: "Update" }) as HTMLButtonElement;
    expect(update.disabled).toBe(true);

    await user.click(within(confirm).getByRole("button", { name: "Retry" }));

    await within(confirm).findByText(/31 indexed files were built with this endpoint/);
    await waitFor(() => expect(update.disabled).toBe(false));
    expect(updateModelEndpointMock).not.toHaveBeenCalled();
  });

  it("tells the server the change was acknowledged", async () => {
    // The server refuses a material edit over indexed files without it; the
    // checkbox is the only place the UI learns the user agreed.
    const user = userEvent.setup();
    renderPage();

    const confirm = await openModelChangeConfirm(user);
    await within(confirm).findByText(/31 indexed files were built with this endpoint/);
    await user.click(within(confirm).getByRole("checkbox"));
    await user.click(within(confirm).getByRole("button", { name: "Update" }));

    await waitFor(() => expect(updateModelEndpointMock).toHaveBeenCalled());
    expect(updateModelEndpointMock.mock.calls[0][2]).toMatchObject({
      model_name: "bge-m3",
      acknowledge_indexed_data: true,
    });
  });

  it("keeps the API-key notice on a copy once its fields settle", async () => {
    // The notice used to share state with validation feedback, which is reset
    // whenever a validated field changes — as every field of a copy does while
    // the form fills in — so it vanished before anyone could read it.
    listModelEndpointsMock.mockResolvedValue([endpoint({ has_api_key: true, extra: { api_key: "********" } })]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Qwen3-Embedding-0.6B");
    await user.click(screen.getByRole("button", { name: /Edit/ }));
    const intro = await screen.findByRole("alertdialog");
    await user.click(within(intro).getByRole("button", { name: "Create a new one" }));

    const form = await screen.findByRole("dialog");
    await within(form).findByDisplayValue("qwen-v2");
    expect(await within(form).findByText(/Its API key is stored server-side and cannot be copied/)).toBeTruthy();
  });

  it("asks for no acknowledgement to change a throughput knob", async () => {
    // Demanding one here is what teaches people to tick it unread.
    const user = userEvent.setup();
    renderPage();

    const form = await openEditForm(user);
    const batchInput = within(form).getByDisplayValue("32");
    await user.clear(batchInput);
    await user.type(batchInput, "64");
    await user.click(within(form).getByRole("button", { name: "Update" }));

    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/Confirm changes/)).toBeTruthy();
    expect(within(confirm).queryByRole("checkbox")).toBeNull();
    expect((within(confirm).getByRole("button", { name: "Update" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(getIndexedUsageMock).not.toHaveBeenCalled();
  });

  it("sends no acknowledgement for a change that moves no vector", async () => {
    const user = userEvent.setup();
    renderPage();

    const form = await openEditForm(user);
    const batchInput = within(form).getByDisplayValue("32");
    await user.clear(batchInput);
    await user.type(batchInput, "64");
    await user.click(within(form).getByRole("button", { name: "Update" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "Update" }));

    await waitFor(() => expect(updateModelEndpointMock).toHaveBeenCalled());
    expect(updateModelEndpointMock.mock.calls[0][2]).not.toHaveProperty("acknowledge_indexed_data");
  });
});

describe("ModelsPage set default embedder (#762)", () => {
  const endpoint = (over: Record<string, unknown> = {}) => ({
    name: "qwen",
    model_type: "embedder" as const,
    endpoint: "https://a.example/v1",
    model_name: "Qwen3-Embedding-0.6B",
    batch_size: 32,
    timeout: 30,
    extra: {},
    has_api_key: false,
    is_default: true,
    used_by_partitions: 2,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...over,
  });

  beforeEach(() => {
    listModelEndpointsMock
      .mockReset()
      .mockResolvedValue([endpoint(), endpoint({ name: "bge", model_name: "bge-m3", is_default: false })]);
    setDefaultMock.mockReset().mockResolvedValue(endpoint({ name: "bge", is_default: true }) as never);
    getIndexedUsageMock.mockReset().mockResolvedValue({
      partitions: [{ partition: "docs", file_count: 31 }],
      total_files: 31,
    } as never);
  });

  it("names the partitions that keep the current default before switching", async () => {
    // "Set default" reads as if it moved everything; indexed partitions stay.
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("bge-m3");
    await user.click(screen.getByRole("button", { name: /Set Default/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Only new partitions, and empty ones that follow the default, will use bge/)).toBeTruthy();
    expect(await within(dialog).findByText("Staying on qwen")).toBeTruthy();
    expect(within(dialog).getByText("docs")).toBeTruthy();
    expect(getIndexedUsageMock).toHaveBeenCalledWith("embedder", "qwen");
    expect(setDefaultMock).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Set default" }));

    await waitFor(() => expect(setDefaultMock).toHaveBeenCalledWith("embedder", "bge"));
  });

  it("switches nothing when cancelled", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("bge-m3");
    await user.click(screen.getByRole("button", { name: /Set Default/ }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(setDefaultMock).not.toHaveBeenCalled();
  });

  it("sets a non-embedder default straight away", async () => {
    // No vectors ride on an LLM, so there is nothing to explain.
    listModelEndpointsMock.mockResolvedValue([
      endpoint({ name: "mistral", model_type: "llm", model_name: "mistral-small" }),
      endpoint({ name: "qwen-chat", model_type: "llm", model_name: "Qwen2.5-7B-Instruct", is_default: false }),
    ]);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("No embedder endpoints configured.");
    await user.click(screen.getByRole("tab", { name: "llm" }));
    await screen.findByText("Qwen2.5-7B-Instruct");
    await user.click(screen.getByRole("button", { name: /Set Default/ }));

    await waitFor(() => expect(setDefaultMock).toHaveBeenCalledWith("llm", "qwen-chat"));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});
