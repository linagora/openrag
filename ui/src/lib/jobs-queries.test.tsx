import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listTasks, type TaskListItem } from "@/lib/api/jobs";
import { JOBS_REFETCH_INTERVAL_MS, jobsQueryKeys, useActiveJobsCount } from "./jobs-queries";

const auth = vi.hoisted(() => ({ user: { id: 7, is_admin: false } as { id: number; is_admin: boolean } | null }));

vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api/jobs", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/jobs")>("@/lib/api/jobs");
  return { ...actual, listTasks: vi.fn() };
});

const listTasksMock = vi.mocked(listTasks);

beforeEach(() => {
  auth.user = { id: 7, is_admin: false };
  vi.resetAllMocks();
});
afterEach(() => vi.useRealTimers());

const task = (taskId: string, userId = 7): TaskListItem => ({
  task_id: taskId,
  state: "QUEUED",
  details: { file_id: `${taskId}-file`, partition: "docs", metadata: {}, user_id: userId },
  url: `/indexer/task/${taskId}`,
});

function createWrapper(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useActiveJobsCount", () => {
  it("counts the server-filtered active tasks", async () => {
    listTasksMock.mockResolvedValue({ tasks: [task("a"), task("b")] });
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current).toBe(2));
    expect(listTasksMock).toHaveBeenCalledWith("active");
  });

  it("does not branch on is_admin", async () => {
    auth.user = { id: 7, is_admin: true };
    listTasksMock.mockResolvedValue({ tasks: [task("a", 1), task("b", 2), task("c", 3)] });
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current).toBe(3));
    expect(listTasksMock).toHaveBeenCalledWith("active");
  });

  it("reports 0 before the first response and while signed out", async () => {
    auth.user = null;
    const { result, rerender } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    expect(result.current).toBe(0);
    expect(listTasksMock).not.toHaveBeenCalled();

    auth.user = { id: 7, is_admin: false };
    listTasksMock.mockResolvedValue({ tasks: [task("a")] });
    rerender();
    expect(result.current).toBe(0);
    await waitFor(() => expect(result.current).toBe(1));
  });

  it("keeps caches apart per account so a re-login cannot inherit a count", async () => {
    listTasksMock.mockResolvedValue({ tasks: [task("a")] });
    const { result, rerender } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });
    await waitFor(() => expect(result.current).toBe(1));

    auth.user = { id: 8, is_admin: false };
    listTasksMock.mockResolvedValue({ tasks: [] });
    rerender();

    expect(result.current).toBe(0);
    expect(jobsQueryKeys.tasks(7, "active")).not.toEqual(jobsQueryKeys.tasks(8, "active"));
  });

  it("refreshes the count every five seconds", async () => {
    vi.useFakeTimers();
    listTasksMock.mockResolvedValue({ tasks: [task("a")] });
    const { unmount } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await act(async () => Promise.resolve());
    expect(listTasksMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(JOBS_REFETCH_INTERVAL_MS));
    expect(listTasksMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("holds the last count through a failed refresh", async () => {
    listTasksMock
      .mockResolvedValueOnce({ tasks: [task("a"), task("b"), task("c"), task("d")] })
      .mockRejectedValue(new Error("temporary failure"));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current).toBe(4));

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: jobsQueryKeys.tasks(7, "active") });
    });

    expect(result.current).toBe(4);
  });
});

describe("jobsQueryKeys", () => {
  it("separates caches by account and status, and stays under the shared prefixes", () => {
    expect(jobsQueryKeys.tasks(7, "ACTIVE")).toEqual(["tasks", 7, "active"]);
    expect(jobsQueryKeys.tasks(7)).toEqual(["tasks", 7, "all"]);
    expect(jobsQueryKeys.queueInfo(7)).toEqual(["queue-info", 7]);
    expect(jobsQueryKeys.tasks(7, "active").slice(0, 1)).toEqual([...jobsQueryKeys.allTasks]);
    expect(jobsQueryKeys.queueInfo(7).slice(0, 1)).toEqual([...jobsQueryKeys.allQueueInfo]);
  });
});
