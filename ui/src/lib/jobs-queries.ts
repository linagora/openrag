import { queryOptions, useQuery, type QueryClient } from "@tanstack/react-query";
import { getQueueInfo, listTasks } from "@/lib/api/jobs";
import { useAuth } from "@/lib/auth";

export const JOBS_REFETCH_INTERVAL_MS = 5_000;

export interface JobsQueryScope {
  userId: number;
  isAdmin: boolean;
}

const scopeKey = ({ userId, isAdmin }: JobsQueryScope) =>
  [userId, isAdmin ? "admin" : "user"] as const;

// The QueryClient outlives account and role changes, so keys must include both
// the user identity and the server-side authorization scope of each response.
export const jobsQueryKeys = {
  allTasks: ["tasks"] as const,
  tasks: (scope: JobsQueryScope, taskStatus?: string) =>
    ["tasks", ...scopeKey(scope), taskStatus?.toLowerCase() ?? "all"] as const,
  allQueueInfo: ["queue-info"] as const,
  queueInfo: (scope: JobsQueryScope) => ["queue-info", ...scopeKey(scope)] as const,
};

export function invalidateJobsQueries(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: jobsQueryKeys.allTasks }),
    queryClient.invalidateQueries({ queryKey: jobsQueryKeys.allQueueInfo }),
  ]);
}

export function jobsTaskListQueryOptions(scope: JobsQueryScope, taskStatus?: string) {
  return queryOptions({
    queryKey: jobsQueryKeys.tasks(scope, taskStatus),
    queryFn: () => listTasks(taskStatus),
    refetchInterval: JOBS_REFETCH_INTERVAL_MS, // poll — OpenRag has no task SSE
    staleTime: JOBS_REFETCH_INTERVAL_MS,
  });
}

export function jobsQueueInfoQueryOptions(scope: JobsQueryScope) {
  return queryOptions({
    queryKey: jobsQueryKeys.queueInfo(scope),
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
  const scope = { userId: user?.id ?? 0, isAdmin: user?.is_admin === true };
  const { data } = useQuery({
    ...jobsTaskListQueryOptions(scope, "active"),
    enabled: !!user,
  });
  return data?.tasks.length ?? 0;
}
