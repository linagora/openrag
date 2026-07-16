import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { getTaskError, getTaskStatus, cancelTask } from "@/lib/api/jobs";
import { copyToClipboard } from "@/lib/utils";
import JobDetailPage from "./detail";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/lib/api/jobs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/jobs")>()),
  getTaskError: vi.fn(),
  getTaskStatus: vi.fn(),
  cancelTask: vi.fn(),
}));

vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  copyToClipboard: vi.fn(),
}));

const getTaskStatusMock = vi.mocked(getTaskStatus);
const getTaskErrorMock = vi.mocked(getTaskError);
const cancelTaskMock = vi.mocked(cancelTask);
const copyToClipboardMock = vi.mocked(copyToClipboard);
const toastSuccessMock = vi.mocked(toast.success);

function renderJobDetail(taskId = "task-1") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/jobs/${taskId}`]}>
        <Routes>
          <Route path="/jobs/:id" element={<JobDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("JobDetailPage failed diagnostics", () => {
  beforeEach(() => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "FAILED",
      details: {
        file_id: "file-1",
        partition: "docs",
        metadata: { filename: "failed.pdf", failed_stage: "chunking" },
        user_id: 1,
      },
    });
    getTaskErrorMock.mockResolvedValue({
      task_id: "task-1",
      traceback: [
        "Traceback (most recent call last):",
        "  File \"worker.py\", line 10, in run",
        "ValueError: parser failed",
      ],
    });
    cancelTaskMock.mockResolvedValue({ message: "cancelled" });
    copyToClipboardMock.mockResolvedValue(true);
    toastSuccessMock.mockClear();
  });

  it("shows readable failed-job diagnostics and copies them", async () => {
    renderJobDetail();

    expect(await screen.findByText("ValueError: parser failed")).not.toBeNull();
    expect(screen.getByText("chunking")).not.toBeNull();
    expect(screen.getByText("Raw traceback")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /copy diagnostics/i }));

    await waitFor(() =>
      expect(copyToClipboardMock).toHaveBeenCalledWith(
        expect.stringContaining("Task ID: task-1"),
        expect.any(HTMLButtonElement),
      ),
    );
    expect(copyToClipboardMock.mock.calls[0][0]).toContain("ValueError: parser failed");
    expect(copyToClipboardMock.mock.calls[0][0]).toContain("Failed stage: chunking");
    expect(toastSuccessMock).toHaveBeenCalledWith("Diagnostics copied to clipboard");
  });
});
