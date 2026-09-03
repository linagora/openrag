import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getMyInfo, type MyInfo } from "@/lib/api/account";
import { getQueueInfo, listTasks, type QueueInfo } from "@/lib/api/jobs";
import {
  JOBS_REFETCH_INTERVAL_MS,
  jobsQueryKeys,
  jobsQueueInfoQueryOptions,
  useActiveJobsCount,
} from "./jobs-queries";

const auth = vi.hoisted(() => ({ user: { id: 7, is_admin: false } as { id: number; is_admin: boolean } | null }));

vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api/account", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/account")>("@/lib/api/account");
  return { ...actual, getMyInfo: vi.fn() };
});
vi.mock("@/lib/api/jobs", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/jobs")>("@/lib/api/jobs");
  return { ...actual, getQueueInfo: vi.fn(), listTasks: vi.fn() };
});

const getMyInfoMock = vi.mocked(getMyInfo);
const getQueueInfoMock = vi.mocked(getQueueInfo);
const listTasksMock = vi.mocked(listTasks);

beforeEach(() => {
  auth.user = { id: 7, is_admin: false };
  vi.resetAllMocks();
});
afterEach(() => vi.useRealTimers());

const myInfo = (pendingFiles: number, userId = 7): MyInfo => ({
  id: userId,
  display_name: "User",
  is_admin: false,
  file_quota: -1,
  pending_files: pendingFiles,
});

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

function createWrapper(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
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

describe("useActiveJobsCount", () => {
  it("uses the lightweight current-user pending count for regular users", async () => {
    getMyInfoMock.mockResolvedValue(myInfo(2));
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current).toBe(2));
    expect(getMyInfoMock).toHaveBeenCalledTimes(1);
    expect(getQueueInfoMock).not.toHaveBeenCalled();
    expect(listTasksMock).not.toHaveBeenCalled();
  });

  it("uses the lightweight global queue summary for administrators", async () => {
    auth.user = { id: 7, is_admin: true };
    getQueueInfoMock.mockResolvedValue(queueInfo(3));
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await waitFor(() => expect(result.current).toBe(3));
    expect(getQueueInfoMock).toHaveBeenCalledTimes(1);
    expect(getMyInfoMock).not.toHaveBeenCalled();
    expect(listTasksMock).not.toHaveBeenCalled();
  });

  it("reports 0 before the first response and while signed out", async () => {
    auth.user = null;
    const { result, rerender } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    expect(result.current).toBe(0);
    expect(getMyInfoMock).not.toHaveBeenCalled();
    expect(getQueueInfoMock).not.toHaveBeenCalled();
    expect(listTasksMock).not.toHaveBeenCalled();

    auth.user = { id: 7, is_admin: false };
    getMyInfoMock.mockResolvedValue(myInfo(1));
    rerender();
    expect(result.current).toBe(0);
    await waitFor(() => expect(result.current).toBe(1));
  });

  it("keeps caches apart per account so a re-login cannot inherit a count", async () => {
    getMyInfoMock.mockResolvedValueOnce(myInfo(1)).mockResolvedValueOnce(myInfo(0, 8));
    const { result, rerender } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });
    await waitFor(() => expect(result.current).toBe(1));

    auth.user = { id: 8, is_admin: false };
    rerender();

    expect(result.current).toBe(0);
    expect(jobsQueryKeys.activeCount({ userId: 7, isAdmin: false })).not.toEqual(
      jobsQueryKeys.activeCount({ userId: 8, isAdmin: false }),
    );
  });

  it("does not expose an administrator count after a same-account role downgrade", async () => {
    auth.user = { id: 7, is_admin: true };
    const regularUserInfo = deferred<Awaited<ReturnType<typeof getMyInfo>>>();
    getQueueInfoMock.mockResolvedValue(queueInfo(3));
    getMyInfoMock.mockReturnValue(regularUserInfo.promise);
    const { result, rerender } = renderHook(() => useActiveJobsCount(), {
      wrapper: createWrapper(),
    });
    await waitFor(() => expect(result.current).toBe(3));

    auth.user = { id: 7, is_admin: false };
    rerender();

    expect(result.current).toBe(0);
    expect(getQueueInfoMock).toHaveBeenCalledTimes(1);
    expect(getMyInfoMock).toHaveBeenCalledTimes(1);
    expect(listTasksMock).not.toHaveBeenCalled();

    regularUserInfo.resolve(myInfo(1));
    await waitFor(() => expect(result.current).toBe(1));
  });

  it("refreshes the count every five seconds", async () => {
    vi.useFakeTimers();
    getMyInfoMock.mockResolvedValue(myInfo(1));
    const { unmount } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper() });

    await act(async () => Promise.resolve());
    expect(getMyInfoMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(JOBS_REFETCH_INTERVAL_MS));
    expect(getMyInfoMock).toHaveBeenCalledTimes(2);
    expect(listTasksMock).not.toHaveBeenCalled();
    unmount();
  });

  it("holds the last count through a failed refresh", async () => {
    getMyInfoMock
      .mockResolvedValueOnce(myInfo(4))
      .mockRejectedValue(new Error("temporary failure"));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useActiveJobsCount(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current).toBe(4));

    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: jobsQueryKeys.activeCount({ userId: 7, isAdmin: false }),
      });
    });

    expect(result.current).toBe(4);
  });

  it("shares the administrator queue request with another observer", async () => {
    auth.user = { id: 7, is_admin: true };
    getQueueInfoMock.mockResolvedValue(queueInfo(3));
    const scope = { userId: 7, isAdmin: true };
    const { result } = renderHook(
      () => {
        const count = useActiveJobsCount();
        const queueQuery = useQuery(jobsQueueInfoQueryOptions(scope));
        return { count, queueActive: queueQuery.data?.tasks.active };
      },
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current).toEqual({ count: 3, queueActive: 3 }));
    expect(getQueueInfoMock).toHaveBeenCalledTimes(1);
  });
});

describe("jobsQueryKeys", () => {
  it("separates caches by account, authorization scope, and status", () => {
    expect(jobsQueryKeys.activeCount({ userId: 7, isAdmin: false })).toEqual([
      "tasks",
      "active-count",
      7,
      "user",
    ]);
    expect(jobsQueryKeys.tasks({ userId: 7, isAdmin: false }, "ACTIVE")).toEqual([
      "tasks",
      7,
      "user",
      "active",
    ]);
    expect(jobsQueryKeys.tasks({ userId: 7, isAdmin: true })).toEqual([
      "tasks",
      7,
      "admin",
      "all",
    ]);
    expect(jobsQueryKeys.queueInfo({ userId: 7, isAdmin: true })).toEqual([
      "queue-info",
      7,
      "admin",
    ]);
  });

  it("keeps scoped entries under the shared invalidation prefixes", () => {
    expect(jobsQueryKeys.activeCount({ userId: 7, isAdmin: false }).slice(0, 1)).toEqual([
      ...jobsQueryKeys.allTasks,
    ]);
    expect(
      jobsQueryKeys.tasks({ userId: 7, isAdmin: false }, "active").slice(0, 1),
    ).toEqual([...jobsQueryKeys.allTasks]);
    expect(jobsQueryKeys.queueInfo({ userId: 7, isAdmin: true }).slice(0, 1)).toEqual([
      ...jobsQueryKeys.allQueueInfo,
    ]);
  });

  it("does not collide active-count data with task-list data", () => {
    const scope = { userId: 7, isAdmin: false };
    expect(jobsQueryKeys.activeCount(scope)).not.toEqual(
      jobsQueryKeys.tasks(scope, "active-count"),
    );
  });
});
