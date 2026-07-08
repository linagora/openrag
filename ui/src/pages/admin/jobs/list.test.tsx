import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listTasks } from "@/lib/api/jobs";
import JobListPage from "./list";

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    isAdmin: true,
  }),
}));

vi.mock("@/lib/api/jobs", () => ({
  listTasks: vi.fn(),
}));

const listTasksMock = vi.mocked(listTasks);

const task = (task_id: string, state: "COMPLETED" | "FAILED", filename: string) => ({
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
});
