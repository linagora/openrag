import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { getQueueInfo, listTasks } from "@/lib/api/jobs";
import { downloadCsv } from "@/lib/csv";
import JobListPage from "./list";

const permissions = vi.hoisted(() => ({ isAdmin: true }));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: permissions.isAdmin,
  }),
}));

vi.mock("@/lib/api/jobs", () => ({
  getQueueInfo: vi.fn(),
  listTasks: vi.fn(),
}));

vi.mock("@/lib/csv", () => ({
  downloadCsv: vi.fn(),
}));

const getQueueInfoMock = vi.mocked(getQueueInfo);
const listTasksMock = vi.mocked(listTasks);
const downloadCsvMock = vi.mocked(downloadCsv);

beforeAll(() => {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => {};
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

const task = (task_id: string, state: "COMPLETED" | "FAILED", filename: string, partition = "docs") => ({
  task_id,
  state,
  details: {
    file_id: task_id,
    partition,
    metadata: { filename },
    user_id: 1,
  },
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

  it("filters jobs by partition and exports the filtered rows", async () => {
    listTasksMock.mockResolvedValue({
      tasks: [
        task("docs-task", "COMPLETED", "docs.pdf", "docs"),
        task("archive-task", "FAILED", "archive.pdf", "archive"),
      ],
    });

    renderJobs();

    expect(await screen.findByText("docs.pdf")).not.toBeNull();
    await userEvent.click(screen.getByRole("combobox", { name: /filter jobs by partition/i }));
    await userEvent.click(await screen.findByRole("option", { name: "archive" }));

    expect(screen.queryByText("docs.pdf")).toBeNull();
    expect(screen.getByText("archive.pdf")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /export csv/i }));

    expect(downloadCsvMock).toHaveBeenCalledWith(
      "openrag-jobs.csv",
      expect.any(Array),
      [expect.objectContaining({ task_id: "archive-task" })],
    );

    await userEvent.click(screen.getByRole("tab", { name: "FAILED" }));
    await waitFor(() => expect(screen.getByText("docs.pdf")).not.toBeNull());
  });
});
