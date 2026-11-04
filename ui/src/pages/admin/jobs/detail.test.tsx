import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { getTaskError, getTaskStatus, cancelTask } from "@/lib/api/jobs";
import { listPartitions, type PartitionResponse } from "@/lib/api/partitions";
import { copyToClipboard } from "@/lib/utils";
import JobDetailPage from "./detail";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const permissions = vi.hoisted(() => ({ isAdmin: true, superAdmin: false }));
const auth = vi.hoisted(() => ({
  user: { id: 7, is_admin: true },
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: permissions.isAdmin,
    superAdminModeResolved: true,
    canRead: (role: string | null | undefined) => permissions.superAdmin || !!role,
  }),
}));

vi.mock("@/lib/api/partitions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/partitions")>()),
  listPartitions: vi.fn(),
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
const listPartitionsMock = vi.mocked(listPartitions);

function partitionRows(...rows: { partition: string; role: string | null }[]) {
  return { partitions: rows as unknown as PartitionResponse[] };
}

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
    permissions.superAdmin = false;
    auth.user = { id: 7, is_admin: true };
    vi.clearAllMocks();
    listPartitionsMock.mockResolvedValue(partitionRows({ partition: "docs", role: "owner" }));
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

  it("uses the complete reason in details and copied diagnostics", async () => {
    const reason = `RuntimeError: ${"x".repeat(700)}`;
    getTaskErrorMock.mockResolvedValue({
      task_id: "task-1",
      reason,
      summary: `${reason.slice(0, 497)}...`,
      traceback: ["RuntimeError: fallback"],
    });

    renderJobDetail();

    expect(await screen.findByText(reason)).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /copy diagnostics/i }));
    await waitFor(() => expect(copyToClipboardMock).toHaveBeenCalled());
    expect(copyToClipboardMock.mock.calls[0][0]).toContain(`Reason: ${reason}`);
  });

  it("keeps the traceback-derived reason when an older server omits the summary", async () => {
    getTaskErrorMock.mockResolvedValue({
      task_id: "task-1",
      traceback: [
        "Traceback (most recent call last):",
        '  File "worker.py", line 10, in run',
        "ValueError: parser failed",
      ],
    } as Awaited<ReturnType<typeof getTaskError>>);

    renderJobDetail();

    expect(await screen.findByText("ValueError: parser failed")).not.toBeNull();
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

  it("does not reuse cached admin diagnostics after a same-account role downgrade", async () => {
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
    auth.user = { id: 7, is_admin: false };
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

  describe("file link", () => {
    beforeEach(() => {
      getTaskStatusMock.mockResolvedValue({
        task_id: "task-1",
        task_state: "COMPLETED",
        details: {
          file_id: "file-1",
          partition: "docs",
          metadata: { filename: "report.pdf" },
          user_id: 2,
        },
      });
    });

    it("links the file when the viewer has a role on its partition", async () => {
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "docs", role: "viewer" }));

      renderJobDetail();

      const link = await screen.findByRole("link", { name: "report.pdf" });
      expect(link.getAttribute("href")).toBe("/documents/docs/file-1");
    });

    it("does not link an admin to a partition they are not a member of when super-admin mode is off", async () => {
      // An admin sees every user's task, but with SUPER_ADMIN_MODE off the API
      // only lets them read partitions they belong to, and GET /partition/
      // returns just those (here: "default", not the task's "docs").
      permissions.isAdmin = true;
      permissions.superAdmin = false;
      auth.user = { id: 1, is_admin: true };
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "default", role: "owner" }));

      renderJobDetail();

      await screen.findByText(
        "You can see this job because you're an admin, but you can't open its file: partition 'docs' no longer exists or you're not a member of it. If it still exists, ask its owner for access, or turn on Super Admin Mode (SUPER_ADMIN_MODE=true) for debugging.",
      );
      expect(screen.getByText("report.pdf").closest("a")).toBeNull();
      expect(screen.queryByRole("link", { name: "report.pdf" })).toBeNull();
    });

    it("tells a regular user who lost access to ask the owner, without the super-admin hint", async () => {
      permissions.isAdmin = false;
      auth.user = { id: 2, is_admin: false };
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "default", role: "owner" }));

      renderJobDetail();

      await screen.findByText(
        "You can't open this file: partition 'docs' no longer exists or you're not a member of it. If it still exists, ask its owner for access.",
      );
      expect(screen.queryByText(/SUPER_ADMIN_MODE/)).toBeNull();
      expect(screen.queryByRole("link", { name: "report.pdf" })).toBeNull();
    });

    it("says the access check failed, not that access is missing, and retries it", async () => {
      listPartitionsMock.mockRejectedValueOnce(new Error("Network down"));
      listPartitionsMock.mockResolvedValueOnce(partitionRows({ partition: "docs", role: "viewer" }));

      renderJobDetail();

      await screen.findByText(/Couldn't check your access to partition 'docs'.*Network down/);
      expect(screen.queryByText(/no longer exists or you're not a member/)).toBeNull();
      expect(screen.queryByRole("link", { name: "report.pdf" })).toBeNull();

      await userEvent.click(screen.getByRole("button", { name: "Retry" }));

      const link = await screen.findByRole("link", { name: "report.pdf" });
      expect(link.getAttribute("href")).toBe("/documents/docs/file-1");
      expect(screen.queryByText(/Couldn't check your access/)).toBeNull();
    });

    it("does not link the file from partitions cached for a previous viewer", async () => {
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "docs", role: "owner" }));
      const { rerenderJobDetail } = renderJobDetail();

      expect(await screen.findByRole("link", { name: "report.pdf" })).not.toBeNull();

      // Another account takes over the same tab (token logout, then login).
      permissions.isAdmin = false;
      auth.user = { id: 8, is_admin: false };
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "default", role: "owner" }));
      rerenderJobDetail();

      expect(screen.queryByRole("link", { name: "report.pdf" })).toBeNull();
      await screen.findByText(/You can't open this file: partition 'docs' no longer exists/);
      expect(listPartitionsMock).toHaveBeenCalledTimes(2);
    });

    it("links any partition's file in super-admin mode", async () => {
      permissions.superAdmin = true;
      listPartitionsMock.mockResolvedValue(partitionRows({ partition: "docs", role: null }));

      renderJobDetail();

      const link = await screen.findByRole("link", { name: "report.pdf" });
      expect(link.getAttribute("href")).toBe("/documents/docs/file-1");
    });
  });
});
