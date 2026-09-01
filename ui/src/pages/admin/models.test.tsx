import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { listModelEndpoints, validateModelEndpoint } from "@/lib/api/models";
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
    validateModelEndpointMock.mockReset().mockResolvedValue({
      reachable: true,
      model_found: true,
      transcription_supported: true,
    });
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
});
