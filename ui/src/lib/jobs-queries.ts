import { queryOptions, useQuery, type QueryClient } from "@tanstack/react-query";
import { getQueueInfo, listTasks } from "@/lib/api/jobs";
import { useAuth } from "@/lib/auth";

export const JOBS_REFETCH_INTERVAL_MS = 5_000;

export interface JobsQueryScope {
  userId: number;
  isAdmin: boolean;
}

const scopeKey = (scope: JobsQueryScope) =>
  [scope.userId, scope.isAdmin ? "admin" : "user"] as const;

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

interface JobsPollingOptions {
  poll: boolean;
}

const pollingInterval = ({ poll }: JobsPollingOptions) =>
  poll ? JOBS_REFETCH_INTERVAL_MS : false;

export function jobsTaskListQueryOptions(
  scope: JobsQueryScope,
  taskStatus: string | undefined,
  polling: JobsPollingOptions,
) {
  return queryOptions({
    queryKey: jobsQueryKeys.tasks(scope, taskStatus),
    queryFn: () => listTasks(taskStatus),
    refetchInterval: pollingInterval(polling),
    staleTime: JOBS_REFETCH_INTERVAL_MS,
  });
}

export function jobsQueueInfoQueryOptions(
  scope: JobsQueryScope,
  polling: JobsPollingOptions,
) {
  return queryOptions({
    queryKey: jobsQueryKeys.queueInfo(scope),
    queryFn: getQueueInfo,
    refetchInterval: pollingInterval(polling),
    staleTime: JOBS_REFETCH_INTERVAL_MS,
  });
}

export interface ActiveJobsCountResult {
  count: number;
  isInitialLoading: boolean;
  hasResolvedOnce: boolean;
  isError: boolean;
}

export function useActiveJobsCount(): ActiveJobsCountResult {
  const { user } = useAuth();
  const userId = user?.id ?? 0;
  const adminQuery = useQuery({
    ...jobsQueueInfoQueryOptions({ userId, isAdmin: true }, { poll: true }),
    enabled: user?.is_admin === true,
  });
  const userQuery = useQuery({
    ...jobsTaskListQueryOptions({ userId, isAdmin: false }, "active", { poll: true }),
    enabled: !!user && !user.is_admin,
  });
  const query = user?.is_admin ? adminQuery : userQuery;
  const count = user?.is_admin ? adminQuery.data?.tasks.active : userQuery.data?.tasks.length;

  return {
    count: count ?? 0,
    isInitialLoading: query.isPending,
    hasResolvedOnce: count !== undefined,
    isError: query.isError,
  };
}
