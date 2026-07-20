import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cancelTask, getQueueInfo, listTasks, type TaskState } from "@/lib/api/jobs";
import JobListPage from "./list";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const permissions = vi.hoisted(() => ({ isAdmin: true }));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: permissions.isAdmin,
  }),
}));

vi.mock("@/lib/api/jobs", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/jobs")>("@/lib/api/jobs");
  return {
    ...actual,
    cancelTask: vi.fn(),
    getQueueInfo: vi.fn(),
    listTasks: vi.fn(),
  };
});

const cancelTaskMock = vi.mocked(cancelTask);
const getQueueInfoMock = vi.mocked(getQueueInfo);
const listTasksMock = vi.mocked(listTasks);
const toastErrorMock = vi.mocked(toast.error);

const task = (
  task_id: string,
  state: TaskState,
  filename: string,
  partition = "docs",
  timing: { created_at?: string; duration_ms?: number } = {},
) => ({
  task_id,
  state,
  details: {
    file_id: task_id,
    partition,
    metadata: { filename },
    user_id: 1,
  },
  ...timing,
  url: `/indexer/task/${task_id}`,
});

function renderJobs() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <JobListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("JobListPage filters", () => {
  beforeEach(() => {
    permissions.isAdmin = true;
    vi.clearAllMocks();
    cancelTaskMock.mockResolvedValue({ message: "Cancellation signal sent" });
    getQueueInfoMock.mockResolvedValue({
      workers: { total_slots: 4, pool_size: 2, max_per_actor: 2 },
      tasks: {
        active: 1,
        active_statuses: { QUEUED: 0, SERIALIZING: 0, CHUNKING: 1, INSERTING: 0 },
        total_completed: 12,
        total_cancelled: 0,
        total_failed: 1,
      },
    });
    listTasksMock.mockImplementation(async (status?: string) => ({
      tasks:
        status === "FAILED"
          ? [task("failed-task", "FAILED", "failed.pdf")]
          : [
              task("completed-task", "COMPLETED", "completed.pdf"),
              task("failed-task", "FAILED", "failed.pdf"),
            ],
    }));
  });

  it("clears search when switching status tabs", async () => {
    renderJobs();

    expect(await screen.findByText("completed.pdf")).not.toBeNull();
    const search = screen.getByPlaceholderText("Search jobs...") as HTMLInputElement;

    await userEvent.type(search, "completed");
    expect(search.value).toBe("completed");

    await userEvent.click(screen.getByRole("tab", { name: "FAILED" }));

    await waitFor(() => expect(search.value).toBe(""));
    expect(await screen.findByText("failed.pdf")).not.toBeNull();
  });

  it("bulk-cancels only selected active jobs", async () => {
    listTasksMock.mockResolvedValue({
      tasks: [
        task("queued-task", "QUEUED", "queued.pdf"),
        task("completed-task", "COMPLETED", "completed.pdf"),
      ],
    });

    renderJobs();

    expect(await screen.findByText("queued.pdf")).not.toBeNull();
    expect(screen.getByText("completed.pdf")).not.toBeNull();

    const rowCheckboxes = screen.getAllByRole("checkbox", { name: /select row/i });
    expect(rowCheckboxes[0].hasAttribute("disabled")).toBe(false);
    expect(rowCheckboxes[1].hasAttribute("disabled")).toBe(true);

    await userEvent.click(screen.getByRole("checkbox", { name: /select visible rows/i }));
    expect(screen.getByText("1 selected")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /cancel selected/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(cancelTaskMock).toHaveBeenCalledWith("queued-task"));
    expect(cancelTaskMock).not.toHaveBeenCalledWith("completed-task");
  });

  it("rechecks selected jobs before bulk cancelling", async () => {
    let tasks = [task("queued-task", "QUEUED", "queued.pdf")];
    listTasksMock.mockImplementation(async () => ({ tasks }));

    renderJobs();

    expect(await screen.findByText("queued.pdf")).not.toBeNull();

    await userEvent.click(screen.getByRole("checkbox", { name: /select row/i }));
    expect(screen.getByText("1 selected")).not.toBeNull();

    tasks = [task("queued-task", "COMPLETED", "queued.pdf")];

    await userEvent.click(screen.getByRole("button", { name: /cancel selected/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith("1 task(s) were no longer active"));
    expect(cancelTaskMock).not.toHaveBeenCalled();
  });

  it("clears selected jobs when the search changes", async () => {
    listTasksMock.mockResolvedValue({
      tasks: [task("queued-task", "QUEUED", "queued.pdf"), task("other-task", "QUEUED", "other.pdf")],
    });

    renderJobs();

    expect(await screen.findByText("queued.pdf")).not.toBeNull();

    await userEvent.click(screen.getAllByRole("checkbox", { name: /select row/i })[0]);
    expect(screen.getByText("1 selected")).not.toBeNull();

    await userEvent.type(screen.getByPlaceholderText("Search jobs..."), "other");

    await waitFor(() => expect(screen.queryByText("1 selected")).toBeNull());
  });

  it("keeps long job values constrained while exposing full names", async () => {
    const longTaskId = "task-" + "1234567890".repeat(5);
    const longFilename = "benchmark-run-with-a-very-long-document-name-that-should-not-stretch-the-table.pdf";
    const longPartition = "benchmark-partition-with-a-very-long-name-for-regression-testing";
    listTasksMock.mockResolvedValue({
      tasks: [task(longTaskId, "COMPLETED", longFilename, longPartition)],
    });

    renderJobs();

    expect(await screen.findByTitle(longTaskId)).not.toBeNull();
    expect(screen.getByTitle(longFilename).className).toContain("truncate");
    expect(screen.getByTitle(longPartition).className).toContain("truncate");
  });

  it("shows task creation time and duration", async () => {
    const createdAt = "2026-07-20T08:00:00+00:00";
    listTasksMock.mockResolvedValue({
      tasks: [task("timed-task", "COMPLETED", "timed.pdf", "docs", { created_at: createdAt, duration_ms: 65_000 })],
    });

    renderJobs();

    expect((await screen.findByTitle(createdAt)).textContent).toBe(new Date(createdAt).toLocaleString());
    expect(screen.getByText("1m 5s")).not.toBeNull();
  });

  it("shows queue and worker pressure from the backend", async () => {
    getQueueInfoMock.mockResolvedValue({
      workers: { total_slots: 2, pool_size: 1, max_per_actor: 2 },
      tasks: {
        active: 4,
        active_statuses: { QUEUED: 2, SERIALIZING: 0, CHUNKING: 2, INSERTING: 0 },
        total_completed: 20,
        total_cancelled: 0,
        total_failed: 1,
      },
    });

    renderJobs();

    expect(await screen.findByLabelText("Queue pressure")).not.toBeNull();
    expect(screen.getByText("Saturated")).not.toBeNull();
    expect(screen.getByText("Queued 2")).not.toBeNull();
    expect(screen.getByText("Running 2/2")).not.toBeNull();
    expect(screen.getByText("Workers 1 x 2")).not.toBeNull();
  });

  it("handles unavailable queue information", async () => {
    getQueueInfoMock.mockRejectedValue(new Error("queue unavailable"));

    renderJobs();

    expect(await screen.findByText("Queue unavailable")).not.toBeNull();
    expect(await screen.findByText("completed.pdf")).not.toBeNull();
  });

  it("does not fetch admin-only queue information for non-admin users", async () => {
    permissions.isAdmin = false;

    renderJobs();

    expect(await screen.findByText("completed.pdf")).not.toBeNull();
    expect(screen.queryByLabelText("Queue pressure")).toBeNull();
    expect(screen.queryByText("Queue unavailable")).toBeNull();
    expect(getQueueInfoMock).not.toHaveBeenCalled();

    const initialTaskRequests = listTasksMock.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Refresh jobs" }));

    await waitFor(() => expect(listTasksMock.mock.calls.length).toBeGreaterThan(initialTaskRequests));
    expect(getQueueInfoMock).not.toHaveBeenCalled();
  });
});
