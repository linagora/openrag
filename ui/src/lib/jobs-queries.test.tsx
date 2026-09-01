import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getQueueInfo,
  listTasks,
  type QueueInfo,
  type TaskListItem,
} from "@/lib/api/jobs";
import {
  JOBS_REFETCH_INTERVAL_MS,
  jobsQueueInfoQueryOptions,
  jobsQueryKeys,
  jobsTaskListQueryOptions,
  useActiveJobsCount,
} from "./jobs-queries";

const auth = vi.hoisted(() => ({
  user: { id: 7, is_admin: true },
}));

vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api/jobs", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/jobs")>("@/lib/api/jobs");
  return { ...actual, getQueueInfo: vi.fn(), listTasks: vi.fn() };
});

const getQueueInfoMock = vi.mocked(getQueueInfo);
const listTasksMock = vi.mocked(listTasks);

beforeEach(() => {
  auth.user = { id: 7, is_admin: true };
  vi.resetAllMocks();
});
afterEach(() => vi.useRealTimers());

const queueInfo = (active: number): QueueInfo => ({
  workers: { total_slots: 4, pool_size: 2, max_per_actor: 2 },
  tasks: {
    active,
    active_statuses: { QUEUED: active, SERIALIZING: 0 },
    total_completed: 20,
    total_cancelled: 1,
    total_failed: 2,
  },
});

const task = (
  taskId: string,
  userId = 7,
  state: TaskListItem["state"] = "QUEUED",
): TaskListItem => ({
  task_id: taskId,
  state,
  details: { file_id: `${taskId}-file`, partition: "docs", metadata: {}, user_id: userId },
  url: `/indexer/task/${taskId}`,
});

const taskList = (...tasks: TaskListItem[]) => ({ tasks });

function createQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function createWrapper(queryClient = createQueryClient()) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function refetchAdminCount(queryClient: QueryClient) {
  await act(async () => {
    await queryClient.refetchQueries({
      queryKey: jobsQueryKeys.queueInfo({ userId: 7, isAdmin: true }),
    });
  });
}

describe("useActiveJobsCount", () => {
  it("uses the queue summary active total for administrators", async () => {
    getQueueInfoMock.mockResolvedValue(queueInfo(12));
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.hasResolvedOnce).toBe(true));

    expect(result.current).toEqual({
      count: 12,
      isInitialLoading: false,
      hasResolvedOnce: true,
      isError: false,
    });
    expect(listTasksMock).not.toHaveBeenCalled();
  });

  it("uses the server-filtered active task list for regular users", async () => {
    auth.user = { id: 7, is_admin: false };
    listTasksMock.mockResolvedValue(taskList(task("queued"), task("running", 7, "SERIALIZING")));
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.hasResolvedOnce).toBe(true));

    expect(result.current.count).toBe(2);
    expect(listTasksMock).toHaveBeenCalledWith("active");
    expect(getQueueInfoMock).not.toHaveBeenCalled();
  });

  it("clears the global count while a downgraded user's scope resolves", async () => {
    getQueueInfoMock.mockResolvedValue(queueInfo(12));
    const userTasks = deferred<Awaited<ReturnType<typeof listTasks>>>();
    listTasksMock.mockReturnValue(userTasks.promise);
    const { result, rerender } = renderHook(() => useActiveJobsCount(), {
      wrapper: createWrapper(),
    });
    await waitFor(() => expect(result.current.count).toBe(12));

    auth.user = { id: 7, is_admin: false };
    rerender();
    expect(result.current).toMatchObject({ count: 0, hasResolvedOnce: false });

    userTasks.resolve(taskList(task("own-task")));
    await waitFor(() => expect(result.current.count).toBe(1));
  });

  it("clears a user count while a promoted administrator's scope resolves", async () => {
    auth.user = { id: 7, is_admin: false };
    listTasksMock.mockResolvedValue(taskList(task("own-task")));
    const adminQueue = deferred<Awaited<ReturnType<typeof getQueueInfo>>>();
    getQueueInfoMock.mockReturnValue(adminQueue.promise);
    const { result, rerender } = renderHook(() => useActiveJobsCount(), {
      wrapper: createWrapper(),
    });
    await waitFor(() => expect(result.current.count).toBe(1));

    auth.user = { id: 7, is_admin: true };
    rerender();
    expect(result.current).toMatchObject({ count: 0, hasResolvedOnce: false });

    adminQueue.resolve(queueInfo(12));
    await waitFor(() => expect(result.current.count).toBe(12));
  });

  it("clears a regular user's count while a different account resolves", async () => {
    auth.user = { id: 7, is_admin: false };
    listTasksMock.mockResolvedValueOnce(taskList(task("first-user-task")));
    const secondUserTasks = deferred<Awaited<ReturnType<typeof listTasks>>>();
    listTasksMock.mockReturnValueOnce(secondUserTasks.promise);
    const { result, rerender } = renderHook(() => useActiveJobsCount(), {
      wrapper: createWrapper(),
    });
    await waitFor(() => expect(result.current.count).toBe(1));

    auth.user = { id: 8, is_admin: false };
    rerender();
    expect(result.current).toMatchObject({ count: 0, hasResolvedOnce: false });

    secondUserTasks.resolve(taskList(task("second-1", 8), task("second-2", 8, "SERIALIZING")));
    await waitFor(() => expect(result.current.count).toBe(2));
  });

  it("refreshes the active count every five seconds", async () => {
    vi.useFakeTimers();
    getQueueInfoMock.mockResolvedValue(queueInfo(3));
    const { unmount } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await act(async () => Promise.resolve());
    expect(getQueueInfoMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(JOBS_REFETCH_INTERVAL_MS));
    expect(getQueueInfoMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("reports an unresolved initial failure without exposing a count", async () => {
    getQueueInfoMock.mockRejectedValue(new Error("queue unavailable"));
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current).toEqual({
      count: 0,
      isInitialLoading: false,
      hasResolvedOnce: false,
      isError: true,
    });
  });

  it("retains the last count on failure and accepts a later successful zero", async () => {
    getQueueInfoMock
      .mockResolvedValueOnce(queueInfo(4))
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce(queueInfo(0));
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useActiveJobsCount(), {
      wrapper: createWrapper(queryClient),
    });
    await waitFor(() => expect(result.current.count).toBe(4));

    await refetchAdminCount(queryClient);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current).toMatchObject({ count: 4, hasResolvedOnce: true, isError: true });

    await refetchAdminCount(queryClient);
    await waitFor(() => expect(result.current.count).toBe(0));
    expect(result.current.hasResolvedOnce).toBe(true);
  });
});

describe("shared Jobs query configuration", () => {
  it("separates caches by stable identity, authorization scope, and status", () => {
    expect(jobsQueryKeys.tasks({ userId: 7, isAdmin: false }, "active")).toEqual([
      "tasks",
      7,
      "user",
      "active",
    ]);
    expect(jobsQueryKeys.tasks({ userId: 7, isAdmin: true }, "ACTIVE")).toEqual([
      "tasks",
      7,
      "admin",
      "active",
    ]);
    expect(jobsQueryKeys.queueInfo({ userId: 7, isAdmin: true })).toEqual([
      "queue-info",
      7,
      "admin",
    ]);
  });

  it("provides one polling policy for active and passive observers", () => {
    const scope = { userId: 7, isAdmin: true };

    expect(jobsQueueInfoQueryOptions(scope, { poll: true }).refetchInterval).toBe(
      JOBS_REFETCH_INTERVAL_MS,
    );
    expect(jobsQueueInfoQueryOptions(scope, { poll: false }).refetchInterval).toBe(false);
    expect(jobsTaskListQueryOptions(scope, "active", { poll: true }).refetchInterval).toBe(
      JOBS_REFETCH_INTERVAL_MS,
    );
    expect(jobsTaskListQueryOptions(scope, "active", { poll: false }).refetchInterval).toBe(false);
  });

  it("keeps one request cycle when a polling and passive observer share a query", async () => {
    vi.useFakeTimers();
    getQueueInfoMock.mockResolvedValue(queueInfo(3));
    const scope = { userId: 7, isAdmin: true };

    renderHook(
      () => {
        useQuery(jobsQueueInfoQueryOptions(scope, { poll: true }));
        useQuery(jobsQueueInfoQueryOptions(scope, { poll: false }));
      },
      { wrapper: createWrapper() },
    );

    await act(async () => Promise.resolve());
    expect(getQueueInfoMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(JOBS_REFETCH_INTERVAL_MS));
    expect(getQueueInfoMock).toHaveBeenCalledTimes(2);
  });
});
