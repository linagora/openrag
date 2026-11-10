import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { getTaskError, getTaskStatus, getTaskStageProgress, cancelTask } from "@/lib/api/jobs";
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
  getTaskStageProgress: vi.fn(),
  cancelTask: vi.fn(),
}));

vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  copyToClipboard: vi.fn(),
}));

const getTaskStatusMock = vi.mocked(getTaskStatus);
const getTaskStageProgressMock = vi.mocked(getTaskStageProgress);
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
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "unknown",
      history_quality: "legacy",
      is_currently_running: false,
      persisted_through_sequence: 0,
      events: [],
      has_more: false,
      next_cursor: null,
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

  it("labels an open stage as last observed when a terminal task has no stage end", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "FAILED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "failed.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "unknown",
      is_currently_running: false,
      persisted_through_sequence: 1,
      current_invocation_id: "invocation-1",
      last_known_stage_id: "embed",
      last_known_stage_label: "Embedding",
      events: [
        {
          event_id: "event-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "embed",
          stage_rank: 5,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Last observed start")).not.toBeNull();
    expect(screen.getByText("Stage history completeness is unknown.")).not.toBeNull();
  });

  it("shows the durable finalizing phase ahead of a retained stage label", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "finalizing.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "unknown",
      phase: "finalizing",
      is_currently_running: false,
      last_known_stage_id: "store",
      last_known_stage_label: "Storing",
      current_invocation_id: "invocation-1",
      persisted_through_sequence: 1,
      events: [],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Last observed: Finalizing · Status unconfirmed")).not.toBeNull();
    expect(screen.queryByText("Last observed: Storing · Status unconfirmed")).toBeNull();
  });

  it("keeps the live activity query polling while the timeline pages through history", async () => {
    const currentSummary = {
      freshness: "live" as const,
      history_quality: "complete" as const,
      phase: "stage",
      stage_id: "embed",
      stage_label: "Embedding",
      is_currently_running: true,
      current_invocation_id: "invocation-1",
      persisted_through_sequence: 1,
      events: [],
      has_more: false,
      next_cursor: null,
    };
    const event = (sequence: number, stage_id: string) => ({
      event_id: `event-${sequence}`,
      task_id: "task-1",
      attempt: 1,
      sequence,
      invocation_id: "invocation-1",
      stage_id,
      stage_rank: sequence,
      status: "started" as const,
      occurred_at: "2026-10-22T10:00:00Z",
    });
    const latestPage = {
      ...currentSummary,
      events: [event(3, "embed")],
      has_more: true,
      next_cursor: { attempt: 1, sequence: 2 },
    };
    const olderPage = {
      ...currentSummary,
      events: [event(1, "parse")],
      has_more: false,
      next_cursor: null,
    };
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "history.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockImplementation(async (_taskId, cursor, limit) => {
      if (limit === 1) return currentSummary;
      return cursor ? olderPage : latestPage;
    });

    const { queryClient, rerenderJobDetail } = renderJobDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Older stages" }));

    await waitFor(() => {
      expect(getTaskStageProgressMock).toHaveBeenCalledWith("task-1", { attempt: 1, sequence: 2 }, 50);
    });
    expect(await screen.findByText("Embedding")).not.toBeNull();
    expect(await screen.findByRole("button", { name: "Newer stages" })).not.toBeNull();

    const currentQuery = queryClient.getQueryCache().find({
      queryKey: ["task-stage-progress-current", "task-1", 7, "admin"],
    });
    const historyQuery = queryClient.getQueryCache().find({
      queryKey: ["task-stage-progress", "task-1", 7, "admin", 1, 2],
    });
    expect(currentQuery).toBeDefined();
    expect(historyQuery).toBeDefined();
    if (currentQuery) {
      const currentRefetchInterval = (currentQuery.options as unknown as {
        refetchInterval?: (query: typeof currentQuery) => unknown;
      }).refetchInterval;
      expect(currentRefetchInterval).toBeTypeOf("function");
      expect(currentRefetchInterval?.(currentQuery)).toBe(3000);
    }
    if (historyQuery) {
      const historyRefetchInterval = (historyQuery.options as unknown as {
        refetchInterval?: (query: typeof historyQuery) => unknown;
      }).refetchInterval;
      expect(historyRefetchInterval).toBeTypeOf("function");
      expect(historyRefetchInterval?.(historyQuery)).toBe(false);
    }

    await userEvent.click(screen.getByRole("button", { name: "Newer stages" }));
    await waitFor(() => {
      expect(queryClient.getQueryData(["task-stage-progress", "task-1", 7, "admin", null, null])).toMatchObject({
        events: [expect.objectContaining({ stage_id: "embed" })],
      });
    });

    await userEvent.click(screen.getByRole("button", { name: "Older stages" }));
    auth.user = { id: 8, is_admin: false };
    permissions.isAdmin = false;
    rerenderJobDetail();

    expect(screen.queryByRole("button", { name: "Newer stages" })).toBeNull();
    await waitFor(() => {
      expect(queryClient.getQueryCache().find({
        queryKey: ["task-stage-progress", "task-1", 8, "user", null, null],
      })).toBeDefined();
    });
  });

  it("refreshes the current stage summary when the task becomes terminal", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "transition.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "complete",
      is_currently_running: false,
      persisted_through_sequence: 1,
      events: [],
      has_more: false,
      next_cursor: null,
    });

    const { queryClient } = renderJobDetail();
    await waitFor(() => {
      expect(getTaskStageProgressMock.mock.calls.filter(([, , limit]) => limit === 1).length).toBeGreaterThan(0);
    });
    const currentQueryCallsBeforeTerminal = getTaskStageProgressMock.mock.calls.filter(([, , limit]) => limit === 1)
      .length;

    queryClient.setQueryData(["task", "task-1", 7, "admin"], {
      task_id: "task-1",
      task_state: "COMPLETED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "transition.pdf" }, user_id: 1 },
    });

    await waitFor(() => {
      expect(getTaskStageProgressMock.mock.calls.filter(([, , limit]) => limit === 1).length).toBe(
        currentQueryCallsBeforeTerminal + 1,
      );
    });
  });

  it("labels an open stage interrupted only when worker loss was confirmed", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "FAILED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "interrupted.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "incomplete",
      is_currently_running: false,
      interrupted_invocation_id: "invocation-1",
      persisted_through_sequence: 1,
      current_invocation_id: "invocation-1",
      last_known_stage_id: "embed",
      last_known_stage_label: "Embedding",
      events: [
        {
          event_id: "event-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "embed",
          stage_rank: 5,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Interrupted — worker stopped before a stage end was observed")).not.toBeNull();
    expect(screen.getByText("Some stage history may be missing.")).not.toBeNull();
  });

  it("does not call a durable stage snapshot running even if it carries a running flag", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "stale.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "unknown",
      is_currently_running: true,
      persisted_through_sequence: 1,
      current_invocation_id: "invocation-1",
      last_known_stage_id: "embed",
      last_known_stage_label: "Embedding",
      events: [
        {
          event_id: "event-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "embed",
          stage_rank: 5,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Last observed start — worker activity is not confirmed")).not.toBeNull();
    expect(await screen.findByText("Last observed: Embedding · Status unconfirmed")).not.toBeNull();
    expect(screen.queryByText("Running")).toBeNull();
    expect(screen.getAllByText("Active", { selector: "[data-slot='badge']" })).toHaveLength(1);
  });

  it("shows the live stage, parser substage, and elapsed time in Task Details state", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "running.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "live",
      history_quality: "complete",
      phase: "stage",
      stage_id: "parse",
      stage_label: "Parsing",
      current_substage_id: "parse.convert",
      current_substage_label: "Converting PDF with PyMuPDF",
      elapsed_ms: 15_000,
      elapsed_sampled_at: new Date().toISOString(),
      is_currently_running: true,
      current_invocation_id: "invocation-1",
      persisted_through_sequence: 1,
      events: [
        {
          event_id: "event-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "parse",
          stage_rank: 0,
          status: "started",
          occurred_at: new Date().toISOString(),
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    const state = await screen.findByText(/Parsing · Converting PDF with PyMuPDF/);
    const badge = state.closest("[data-slot='badge']");
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain("15s");
    expect(badge?.className).toContain("bg-amber-100");
  });

  it("labels an open stage running when the snapshot has live worker evidence", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "running.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "live",
      history_quality: "complete",
      is_currently_running: true,
      current_invocation_id: "invocation-1",
      persisted_through_sequence: 1,
      events: [
        {
          event_id: "event-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "embed",
          stage_rank: 5,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Running")).not.toBeNull();
  });

  it("shows a live between-stages phase without calling it last observed", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "SERIALIZING",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "between.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "live",
      history_quality: "complete",
      phase: "between_stages",
      is_currently_running: false,
      last_known_stage_id: "parse",
      last_known_stage_label: "Parsing",
      current_invocation_id: "invocation-1",
      persisted_through_sequence: 2,
      events: [],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    const state = await screen.findByText("Between stages");
    expect(state.textContent).not.toContain("Last observed");
    expect(state.textContent).not.toContain("Status unconfirmed");
    expect(state.textContent).not.toContain("Parsing");
  });

  it("shows a conditional enrichment failure without inventing a skipped stage", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "COMPLETED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "degraded.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "complete",
      is_currently_running: false,
      persisted_through_sequence: 2,
      events: [
        {
          event_id: "event-start",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-caption",
          stage_id: "caption",
          stage_rank: 1,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
        {
          event_id: "event-failed",
          task_id: "task-1",
          attempt: 1,
          sequence: 2,
          invocation_id: "invocation-caption",
          stage_id: "caption",
          stage_rank: 1,
          status: "failed",
          occurred_at: "2026-10-22T10:00:02Z",
          duration_ms: 2000,
          failure_effect: "continued_degraded",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Failed; indexing continued without this enrichment")).not.toBeNull();
    expect(screen.getAllByText("caption").length).toBeGreaterThan(0);
    expect(screen.queryByText(/skipped/i)).toBeNull();
  });

  it("keeps polling terminal tasks while stage history is pending persistence", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    try {
      getTaskStatusMock.mockResolvedValue({
        task_id: "task-1",
        task_state: "COMPLETED",
        details: { file_id: "file-1", partition: "docs", metadata: { filename: "done.pdf" }, user_id: 1 },
      });
      getTaskStageProgressMock.mockResolvedValue({
        freshness: "durable",
        history_quality: "pending_persistence",
        is_currently_running: false,
        persisted_through_sequence: 1,
        events: [],
        has_more: false,
        next_cursor: null,
      });

      const { queryClient } = renderJobDetail();

      expect(await screen.findByText("Recent stage updates may still be saving.")).not.toBeNull();
      const stageQuery = queryClient.getQueryCache().find({
        queryKey: ["task-stage-progress", "task-1", 7, "admin", null, null],
      });
      if (!stageQuery) throw new Error("Stage history query was not created");
      const interval = (stageQuery.options as unknown as { refetchInterval?: unknown }).refetchInterval;
      expect(typeof interval).toBe("function");
      const getInterval = interval as (query: unknown) => number | false;
      expect(getInterval(stageQuery)).toBe(3000);

      const currentQuery = queryClient.getQueryCache().find({
        queryKey: ["task-stage-progress-current", "task-1", 7, "admin"],
      });
      if (!currentQuery) throw new Error("Current stage summary query was not created");
      const currentInterval = (currentQuery.options as unknown as { refetchInterval?: unknown }).refetchInterval;
      expect(typeof currentInterval).toBe("function");
      const getCurrentInterval = currentInterval as (query: unknown) => number | false;
      expect(getCurrentInterval(currentQuery)).toBe(3000);

      now.mockReturnValue(1_031_000);
      expect(getInterval(stageQuery)).toBe(false);
      expect(getCurrentInterval(currentQuery)).toBe(false);
    } finally {
      now.mockRestore();
    }
  });

  it("stops the terminal history poll when persistence completes", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "COMPLETED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "done.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "pending_persistence",
      is_currently_running: false,
      persisted_through_sequence: 1,
      events: [],
      has_more: false,
      next_cursor: null,
    });

    const { queryClient } = renderJobDetail();
    expect(await screen.findByText("Recent stage updates may still be saving.")).not.toBeNull();
    const stageQuery = queryClient.getQueryCache().find({
      queryKey: ["task-stage-progress", "task-1", 7, "admin", null, null],
    });
    if (!stageQuery) throw new Error("Stage history query was not created");
    const interval = (stageQuery.options as unknown as { refetchInterval?: unknown }).refetchInterval;
    if (typeof interval !== "function") throw new Error("Stage history polling interval was not configured");
    expect((interval as (query: unknown) => number | false)(stageQuery)).toBe(3000);

    queryClient.setQueryData(stageQuery.queryKey, {
      freshness: "durable",
      history_quality: "complete",
      is_currently_running: false,
      persisted_through_sequence: 2,
      events: [],
      has_more: false,
      next_cursor: null,
    });

    expect((interval as (query: unknown) => number | false)(stageQuery)).toBe(false);
    expect(await screen.findByText("All observed stage events are saved.")).not.toBeNull();
  });

  it("labels events from a retried attempt in the stage timeline", async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: "task-1",
      task_state: "COMPLETED",
      details: { file_id: "file-1", partition: "docs", metadata: { filename: "retried.pdf" }, user_id: 1 },
    });
    getTaskStageProgressMock.mockResolvedValue({
      freshness: "durable",
      history_quality: "complete",
      is_currently_running: false,
      persisted_through_sequence: 2,
      events: [
        {
          event_id: "attempt-1",
          task_id: "task-1",
          attempt: 1,
          sequence: 1,
          invocation_id: "invocation-1",
          stage_id: "parse",
          stage_rank: 0,
          status: "started",
          occurred_at: "2026-10-22T10:00:00Z",
        },
        {
          event_id: "attempt-2",
          task_id: "task-1",
          attempt: 2,
          sequence: 1,
          invocation_id: "invocation-2",
          stage_id: "parse",
          stage_rank: 0,
          status: "started",
          occurred_at: "2026-10-22T10:01:00Z",
        },
      ],
      has_more: false,
      next_cursor: null,
    });

    renderJobDetail();

    expect(await screen.findByText("Attempt 2")).not.toBeNull();
  });
});
