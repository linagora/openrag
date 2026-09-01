import { queryOptions, useQuery, type QueryClient } from "@tanstack/react-query";
import { getQueueInfo, listTasks } from "@/lib/api/jobs";
import { useAuth } from "@/lib/auth";

export const JOBS_REFETCH_INTERVAL_MS = 5_000;

// Keys carry the user id because the QueryClient outlives a token-mode logout:
// without it, switching accounts in the same tab shows the previous user's tasks.
export const jobsQueryKeys = {
  allTasks: ["tasks"] as const,
  tasks: (userId: number, taskStatus?: string) =>
    ["tasks", userId, taskStatus?.toLowerCase() ?? "all"] as const,
  allQueueInfo: ["queue-info"] as const,
  queueInfo: (userId: number) => ["queue-info", userId] as const,
};

export function invalidateJobsQueries(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: jobsQueryKeys.allTasks }),
    queryClient.invalidateQueries({ queryKey: jobsQueryKeys.allQueueInfo }),
  ]);
}

export function jobsTaskListQueryOptions(userId: number, taskStatus?: string) {
  return queryOptions({
    queryKey: jobsQueryKeys.tasks(userId, taskStatus),
    queryFn: () => listTasks(taskStatus),
    refetchInterval: JOBS_REFETCH_INTERVAL_MS, // poll — OpenRag has no task SSE
    staleTime: JOBS_REFETCH_INTERVAL_MS,
  });
}

export function jobsQueueInfoQueryOptions(userId: number) {
  return queryOptions({
    queryKey: jobsQueryKeys.queueInfo(userId),
    queryFn: getQueueInfo,
    refetchInterval: JOBS_REFETCH_INTERVAL_MS,
    staleTime: JOBS_REFETCH_INTERVAL_MS,
  });
}

/**
 * Active tasks the caller may see — 0 until the first response lands. GET
 * /queue/tasks is role-scoped server-side (own tasks for a user, all tasks for
 * an admin), so no client-side `is_admin` branch is needed to keep it authorized.
 */
export function useActiveJobsCount(): number {
  const { user } = useAuth();
  const { data } = useQuery({
    ...jobsTaskListQueryOptions(user?.id ?? 0, "active"),
    enabled: !!user,
  });
  return data?.tasks.length ?? 0;
}
