import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { listModelEndpoints, updateModelEndpoint, validateModelEndpoint } from "@/lib/api/models";
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
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

const listModelEndpointsMock = vi.mocked(listModelEndpoints);
const updateModelEndpointMock = vi.mocked(updateModelEndpoint);
const validateModelEndpointMock = vi.mocked(validateModelEndpoint);

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
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

describe("ModelsPage STT validation", () => {
  beforeEach(() => {
    listModelEndpointsMock.mockReset().mockResolvedValue([]);
    updateModelEndpointMock.mockReset();
    validateModelEndpointMock.mockReset().mockResolvedValue({
      reachable: true,
      model_found: true,
      transcription_supported: true,
    });
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
