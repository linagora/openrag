import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listAllPrompts } from "@/lib/api/prompts";
import type { PromptResponse } from "@/lib/api/prompts";
import PromptsPage from "./prompts";

vi.mock("@/lib/api/prompts", () => ({
  listAllPrompts: vi.fn(),
  createPrompt: vi.fn(),
  updatePrompt: vi.fn(),
  deletePrompt: vi.fn(),
  setPromptDefault: vi.fn(),
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

const listAllPromptsMock = vi.mocked(listAllPrompts);

function makeAsrPrompt(overrides: Partial<PromptResponse> = {}): PromptResponse {
  return {
    id: "asr-default",
    prompt_type: "asr_transcription",
    name: "default-asr",
    content: "",
    is_default: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    used_by: 0,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PromptsPage />
    </QueryClientProvider>,
  );
}

describe("PromptsPage ASR prompt scope", () => {
  beforeEach(() => {
    listAllPromptsMock.mockReset();
  });

  it("explains inherited and explicitly selected ASR prompts differently", async () => {
    listAllPromptsMock.mockResolvedValue([
      makeAsrPrompt(),
      makeAsrPrompt({ id: "asr-custom", name: "meeting-notes", content: "Keep speaker labels.", is_default: false, used_by: 2 }),
    ]);
    const user = userEvent.setup();

    renderPage();

    const defaultCard = (await screen.findByText("default-asr")).closest("div[class*='relative']")!;
    await user.click(within(defaultCard).getByRole("button", { name: "Edit", exact: true }));
    expect(
      screen.getByText(/used by direct extraction and indexation presets without an explicit ASR prompt selection/i),
    ).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const customCard = screen.getByText("meeting-notes").closest("div[class*='relative']")!;
    await user.click(within(customCard).getByRole("button", { name: "Edit", exact: true }));
    expect(
      screen.getByText(/used only by indexation presets that explicitly select this prompt/i),
    ).toBeTruthy();
  });
});
