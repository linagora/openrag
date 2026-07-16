import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cancelTask, listTasks, type TaskState } from "@/lib/api/jobs";
import JobListPage from "./list";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: true,
  }),
}));

vi.mock("@/lib/api/jobs", () => ({
  cancelTask: vi.fn(),
  isActiveState: (state: string) => ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"].includes(state),
  isTerminalState: (state: string) => ["COMPLETED", "FAILED", "CANCELLED"].includes(state),
  listTasks: vi.fn(),
}));

const cancelTaskMock = vi.mocked(cancelTask);
const listTasksMock = vi.mocked(listTasks);

const task = (task_id: string, state: TaskState, filename: string) => ({
  task_id,
  state,
  details: {
    file_id: task_id,
    partition: "docs",
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
    cancelTaskMock.mockResolvedValue({ message: "Cancellation signal sent" });
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
});
