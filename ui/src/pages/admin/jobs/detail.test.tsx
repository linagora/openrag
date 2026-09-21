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

const permissions = vi.hoisted(() => ({ isAdmin: true }));
const auth = vi.hoisted(() => ({
  user: { id: 7, is_admin: true },
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({ isAdmin: permissions.isAdmin }),
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => auth,
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

function jobDetailTree(queryClient: QueryClient, taskId: string) {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/jobs/${taskId}`]}>
        <Routes>
          <Route path="/jobs/:id" element={<JobDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderJobDetail(taskId = "task-1") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const rendered = render(jobDetailTree(queryClient, taskId));
  return {
    ...rendered,
    queryClient,
    rerenderJobDetail: () => rendered.rerender(jobDetailTree(queryClient, taskId)),
  };
}

describe("JobDetailPage", () => {
  beforeEach(() => {
    permissions.isAdmin = true;
    auth.user = { id: 7, is_admin: true };
    vi.clearAllMocks();
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "FAILED",
      details: {
        file_id: "file-1",
        partition: "docs",
        failed_stage: "chunking",
        metadata: { filename: "failed.pdf" },
        user_id: 1,
      },
    });
    getTaskErrorMock.mockResolvedValue({
      task_id: "task-1",
      summary: "ValueError: canonical parser failure",
      traceback: [
        "Traceback (most recent call last):",
        "  File \"worker.py\", line 10, in run",
        "ValueError: parser failed",
      ],
    });
    cancelTaskMock.mockResolvedValue({ message: "cancelled" });
    copyToClipboardMock.mockResolvedValue(true);
  });

  it("shows readable failed-job diagnostics and copies them", async () => {
    renderJobDetail();

    expect(await screen.findByText("ValueError: canonical parser failure")).not.toBeNull();
    expect(screen.getByText("chunking")).not.toBeNull();
    expect(screen.getByText("Raw traceback")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /copy diagnostics/i }));

    await waitFor(() =>
      expect(copyToClipboardMock).toHaveBeenCalledWith(
        expect.stringContaining("Task ID: task-1"),
        expect.any(HTMLButtonElement),
      ),
    );
    expect(copyToClipboardMock.mock.calls[0][0]).toContain("ValueError: canonical parser failure");
    expect(copyToClipboardMock.mock.calls[0][0]).toContain("Failed stage: chunking");
    expect(toastSuccessMock).toHaveBeenCalledWith("Diagnostics copied to clipboard");
  });

  it("does not treat user metadata as the failed stage", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "FAILED",
      details: {
        file_id: "file-1",
        partition: "docs",
        metadata: { filename: "failed.pdf", stage: "draft", failed_stage: "user-tag" },
        user_id: 1,
      },
    });

    renderJobDetail();

    expect(await screen.findByText("ValueError: canonical parser failure")).not.toBeNull();
    expect(screen.queryByText("draft")).toBeNull();
    expect(screen.queryByText("user-tag")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /copy diagnostics/i }));

    await waitFor(() => expect(copyToClipboardMock).toHaveBeenCalled());
    expect(copyToClipboardMock.mock.calls[0][0]).not.toContain("Failed stage:");
    expect(copyToClipboardMock.mock.calls[0][0]).not.toContain("draft");
    expect(copyToClipboardMock.mock.calls[0][0]).not.toContain("user-tag");
  });

  it("does not reuse cached admin diagnostics after an account change", async () => {
    getTaskErrorMock.mockImplementation(async () =>
      auth.user.is_admin
        ? {
            task_id: "task-1",
            summary: "ValueError: internal parser failure",
            traceback: ["ValueError: internal parser failure"],
          }
        : {
            task_id: "task-1",
            summary: "Task failed. Contact an administrator for details.",
            traceback: ["Task failed. Contact an administrator for details."],
          },
    );
    const { rerenderJobDetail } = renderJobDetail();

    expect((await screen.findAllByText("ValueError: internal parser failure")).length).toBeGreaterThan(0);

    permissions.isAdmin = false;
    auth.user = { id: 8, is_admin: false };
    rerenderJobDetail();

    expect(
      (await screen.findAllByText("Task failed. Contact an administrator for details.")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryAllByText("ValueError: internal parser failure")).toHaveLength(0);
    expect(getTaskErrorMock).toHaveBeenCalledTimes(2);
  });

  it("distinguishes completed jobs with degraded enrichment", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "COMPLETED",
      details: {
        file_id: "file-1",
        partition: "docs",
        metadata: { filename: "degraded.pdf" },
        user_id: 1,
        degraded_stages: ["caption", "contextualize"],
      },
    });

    renderJobDetail();

    expect((await screen.findAllByText("Completed with degradation")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Caption, Contextualization").length).toBeGreaterThan(0);
  });
});
