import { queryOptions, useQuery, type QueryClient } from "@tanstack/react-query";
import { getMyInfo } from "@/lib/api/account";
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
  activeCount: (scope: JobsQueryScope) =>
    ["tasks", "active-count", ...scopeKey(scope)] as const,
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

function jobsUserActiveCountQueryOptions(scope: JobsQueryScope) {
  return queryOptions({
    queryKey: jobsQueryKeys.activeCount(scope),
    queryFn: async () => (await getMyInfo()).pending_files ?? 0,
    refetchInterval: JOBS_REFETCH_INTERVAL_MS,
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
 * Active tasks the caller may see — 0 until the first response lands. Admins
 * reuse the global queue summary; regular users reuse the lightweight pending
 * count already returned with their current-user information.
 */
export function useActiveJobsCount(): number {
  const { user } = useAuth();
  const scope = { userId: user?.id ?? 0, isAdmin: user?.is_admin === true };
  const adminQuery = useQuery({
    ...jobsQueueInfoQueryOptions(scope),
    enabled: !!user && scope.isAdmin,
  });
  const userQuery = useQuery({
    ...jobsUserActiveCountQueryOptions(scope),
    enabled: !!user && !scope.isAdmin,
  });
  return scope.isAdmin ? (adminQuery.data?.tasks.active ?? 0) : (userQuery.data ?? 0);
}
