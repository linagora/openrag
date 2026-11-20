import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef, RowSelectionState } from "@tanstack/react-table";
import { Ban, Download, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/lib/auth";
import { usePermissions } from "@/lib/permissions";

import { PageHeader } from "@/components/shared/page-header";
import { QueryErrorNotice } from "@/components/shared/query-error-notice";
import { DataTable, SortableHeader } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
import { defaultStageBadgeColor, StageElapsedTime, stageBadgeColors } from "@/components/shared/stage-progress";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { cancelTask, isActiveState, listTasks, type QueueInfo, type TaskListItem } from "@/lib/api/jobs";
import { ApiError } from "@/lib/api/client";
import { downloadCsv } from "@/lib/csv";
import {
  invalidateJobsQueries,
  jobsQueueInfoQueryOptions,
  jobsTaskListQueryOptions,
} from "@/lib/jobs-queries";
import { formatDate } from "@/lib/utils";

// OpenRag exposes per-file indexing tasks (TaskStateManager), not batch "jobs".
const STATUS_TABS = ["ALL", "ACTIVE", "COMPLETED", "FAILED", "CANCELLED"] as const;
const JOB_SEARCH_DEBOUNCE_MS = 250;
const ACTIVE_JOBS_PAGE_SIZE = 10;
const ALL_PARTITIONS_FILTER = "__openrag/all_partitions__";

const str = (v: unknown) => (v == null ? "" : String(v));

function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 0) return "—";

  const totalSeconds = Math.floor(durationMs / 1000);
  if (totalSeconds < 1) return "< 1s";

  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) return `${hours}h${minutes > 0 ? ` ${minutes}m` : ""}`;
  if (minutes > 0) return `${minutes}m${seconds > 0 ? ` ${seconds}s` : ""}`;
  return `${seconds}s`;
}

const stageIdsByLabel: Record<string, string> = {
  parsing: "parse",
  captioning: "caption",
  "image captioning": "caption",
  chunking: "chunk",
  contextualizing: "contextualize",
  contextualization: "contextualize",
  "topic tagging": "topic_tag",
  embedding: "embed",
  storing: "store",
};

const stageLabelsById: Record<string, string> = {
  parse: "Parsing",
  caption: "Image captioning",
  chunk: "Chunking",
  contextualize: "Contextualization",
  topic_tag: "Topic tagging",
  embed: "Embedding",
  store: "Storing",
};

function stageIdForLabel(label: string | null | undefined): string | null {
  return label ? stageIdsByLabel[label.trim().toLowerCase()] ?? null : null;
}

function stageLabelForId(stageId: string | null | undefined): string | null {
  return stageId ? stageLabelsById[stageId] ?? null : null;
}

function stageProgressDisplay(task: TaskListItem): {
  stage: string;
  quality: string | null;
  stageId: string | null;
  elapsedMs: number | null;
  elapsedSampledAt: string | null;
} | null {
  const progress = task.stage_progress;
  if (!progress || task.state === "QUEUED") return null;

  const label = progress.stage_label
    || progress.last_known_stage_label
    || stageLabelForId(progress.stage_id)
    || stageLabelForId(progress.last_known_stage_id);
  const liveStage = progress.freshness === "live" && progress.is_currently_running && !!(
    progress.stage_label || progress.stage_id
  );
  const live = progress.freshness === "live";
  const activeWithoutLiveConfirmation = isActiveState(task.state) && !live;
  const activeStageWithoutLiveConfirmation = isActiveState(task.state) && !liveStage;
  const interrupted = !!progress.interrupted_invocation_id &&
    progress.interrupted_invocation_id === progress.current_invocation_id;
  const hasCurrentPhase = ["preparing", "between_stages", "finalizing"].includes(progress.phase ?? "");
  const stageHasNoSeparatePhase = !hasCurrentPhase;
  const statusUnconfirmed = !interrupted && (
    (activeStageWithoutLiveConfirmation && !!label && stageHasNoSeparatePhase)
    || (activeWithoutLiveConfirmation && hasCurrentPhase)
  );
  let stage = isActiveState(task.state) ? "Waiting for stage update…" : "Stage history unavailable";
  if (interrupted) {
    stage = label ? `Interrupted: ${label}` : "Interrupted before a stage was recorded";
  } else if (liveStage) {
    stage = progress.stage_label ?? label ?? "Waiting for stage update…";
  } else if (progress.phase === "finalizing") {
    stage = live || activeWithoutLiveConfirmation ? "Finalizing" : "Last observed: Finalizing";
  } else if (progress.phase === "between_stages") {
    stage = live || activeWithoutLiveConfirmation ? "Between stages" : "Last observed: Between stages";
  } else if (progress.phase === "preparing") {
    stage = live || activeWithoutLiveConfirmation ? "Preparing" : "Last observed: Preparing";
  } else if (label) {
    stage = activeStageWithoutLiveConfirmation ? label : `Last observed: ${label}`;
  } else if (progress.history_quality === "not_started") {
    stage = "No stage history recorded";
  }

  const substageIsParse = progress.stage_id === "parse" || progress.last_known_stage_id === "parse";
  if (substageIsParse && progress.current_substage_label) {
    const hasCount = progress.current_substage_progress != null && progress.current_substage_total != null;
    const count = hasCount
      ? ` (${progress.current_substage_progress}/${progress.current_substage_total})`
      : "";
    stage = `${stage} · ${progress.current_substage_label}${count}`;
  }
  if (statusUnconfirmed && !interrupted) {
    stage = `${stage} · Status unconfirmed`;
  }

  const quality = progress.history_quality === "pending_persistence"
    ? "Saving history"
    : progress.history_quality === "incomplete"
      ? "History incomplete"
      : progress.history_quality === "truncated"
        ? "Older history trimmed"
        : null;
  const stageId = liveStage
    ? progress.stage_id ?? stageIdForLabel(progress.stage_label)
    : interrupted || (
        !!label && !["preparing", "between_stages", "finalizing"].includes(progress.phase ?? "")
      )
      ? progress.stage_id ?? progress.last_known_stage_id ?? stageIdForLabel(label)
      : null;
  return {
    stage,
    quality,
    stageId,
    elapsedMs: liveStage ? progress.elapsed_ms ?? null : null,
    elapsedSampledAt: liveStage ? progress.elapsed_sampled_at ?? null : null,
  };
}

function QueuePressureSummary({
  queueInfo,
  isLoading,
  isLoadingError,
  isRefetchError,
  isFetching,
  onRetry,
}: {
  queueInfo?: QueueInfo;
  isLoading: boolean;
  isLoadingError: boolean;
  isRefetchError: boolean;
  isFetching: boolean;
  onRetry: () => void;
}) {
  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Queue: loading...</p>;
  }
  if (isLoadingError || !queueInfo) {
    return (
      <div className="flex items-center gap-2">
        <p className="text-sm text-muted-foreground" title="Queue information is currently unavailable">
          Queue unavailable
        </p>
        <Button variant="outline" size="sm" aria-label="Retry queue information" onClick={onRetry} disabled={isFetching}>
          {isFetching ? "Retrying…" : "Retry"}
        </Button>
      </div>
    );
  }

  const totalSlots = queueInfo.workers.total_slots;
  const queued = queueInfo.tasks.active_statuses.QUEUED ?? 0;
  const running = Math.max(0, queueInfo.tasks.active - queued);
  const saturated = totalSlots > 0 && queued > 0 && running >= totalSlots;
  const busy = running > 0 || queued > 0;
  const status = saturated ? "Saturated" : busy ? "Busy" : "Idle";

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground" aria-label="Queue pressure">
        <Badge variant="outline" className={saturated ? "text-destructive" : undefined}>
          {status}
        </Badge>
        <span>Queued {queued}</span>
        <span>Running {running}/{totalSlots}</span>
        <span>Workers {queueInfo.workers.pool_size} x {queueInfo.workers.max_per_actor}</span>
      </div>
      {isRefetchError && (
        <QueryErrorNotice
          title="Queue information could not be refreshed"
          message="Showing the previously loaded queue summary. It may be out of date."
          isRetrying={isFetching}
          onRetry={onRetry}
          stale
        />
      )}
    </div>
  );
}

function TruncatedValue({ value, className = "max-w-[320px]" }: { value: string; className?: string }) {
  if (!value) return "—";
  return (
    <span className={`block truncate ${className}`} title={value}>
      {value}
    </span>
  );
}

function TaskStateCell({ task, showActiveState }: { task: TaskListItem; showActiveState: boolean }) {
  const state = <StatusBadge status={task.state} />;
  const progress = showActiveState ? null : stageProgressDisplay(task);
  const hideActiveState = !showActiveState && task.state === "SERIALIZING";
  const progressLabel = progress && (
    <>
      <span>{progress.stage}</span>
      {progress.elapsedMs !== null && (
        <>
          {" · "}
          <StageElapsedTime
            key={`${progress.stageId ?? progress.stage}:${progress.elapsedSampledAt ?? ""}`}
            elapsedMs={progress.elapsedMs}
            elapsedSampledAt={progress.elapsedSampledAt}
          />
        </>
      )}
    </>
  );

  return (
    <div className="flex min-w-32 flex-col items-start gap-1">
      {!hideActiveState && state}
      {progress ? (
        <div className="text-xs text-muted-foreground">
          {progress.stageId ? (
            <Badge
              variant="outline"
              className={stageBadgeColors[progress.stageId] ?? defaultStageBadgeColor}
            >
              {progressLabel}
            </Badge>
          ) : (
            <span>{progressLabel}</span>
          )}
          {progress.quality && <span className="block">{progress.quality}</span>}
        </div>
      ) : hideActiveState ? (
        task.state === "QUEUED"
          ? state
          : <span className="text-xs text-muted-foreground">Waiting for stage update…</span>
      ) : null}
    </div>
  );
}

const taskStateColumn = (showActiveState: boolean): ColumnDef<TaskListItem, unknown> => ({
  accessorKey: "state",
  header: "State",
  cell: ({ row }) => <TaskStateCell task={row.original} showActiveState={showActiveState} />,
});

const columns: ColumnDef<TaskListItem, unknown>[] = [
  {
    accessorKey: "task_id",
    header: "Task",
    cell: ({ row }) => (
      <Link
        to={`/jobs/${row.original.task_id}`}
        className="text-primary hover:underline font-mono text-sm"
        title={row.original.task_id}
      >
        {row.original.task_id.slice(0, 8)}
      </Link>
    ),
  },
  taskStateColumn(true),
  {
    id: "file",
    accessorFn: (task) =>
      (str(task.details?.metadata?.filename) || str(task.details?.file_id)).toLowerCase(),
    header: ({ column }) => <SortableHeader column={column} title="File" />,
    cell: ({ row }) => (
      <TruncatedValue
        value={str(row.original.details?.metadata?.filename) || str(row.original.details?.file_id)}
        className="max-w-[280px] sm:max-w-[360px] lg:max-w-[440px]"
      />
    ),
  },
  {
    id: "partition",
    header: "Partition",
    cell: ({ row }) => (
      <TruncatedValue value={str(row.original.details?.partition)} className="max-w-[180px] sm:max-w-[240px]" />
    ),
  },
  {
    accessorKey: "created_at",
    header: ({ column }) => <SortableHeader column={column} title="Created" />,
    cell: ({ row }) => (
      <span className="whitespace-nowrap" title={row.original.created_at ?? undefined}>
        {formatDate(row.original.created_at)}
      </span>
    ),
  },
  {
    accessorKey: "duration_ms",
    header: "Duration",
    cell: ({ row }) => <span className="whitespace-nowrap tabular-nums">{formatDuration(row.original.duration_ms)}</span>,
  },
];

const failureReasonColumn: ColumnDef<TaskListItem, unknown> = {
  accessorKey: "error_summary",
  header: "Failure reason",
  cell: ({ row }) => (
    <TruncatedValue value={row.original.error_summary ?? ""} className="max-w-[240px] lg:max-w-[320px]" />
  ),
};

const adminColumns = [columns[0], columns[1], failureReasonColumn, ...columns.slice(2)];
const activeColumns: ColumnDef<TaskListItem, unknown>[] = [
  columns[0],
  taskStateColumn(false),
  ...columns.slice(2),
];
const adminActiveColumns = [activeColumns[0], activeColumns[1], failureReasonColumn, ...activeColumns.slice(2)];

export default function JobListPage() {
  const { isAdmin } = usePermissions();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [statusTab, setStatusTab] = useState<string>("ALL");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [partitionFilter, setPartitionFilter] = useState(ALL_PARTITIONS_FILTER);
  const [activePageIndex, setActivePageIndex] = useState(0);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const taskStatusFilter = statusTab === "ALL" ? undefined : statusTab === "ACTIVE" ? "active" : statusTab;
  const requestServerPage =
    statusTab === "ACTIVE" && !debouncedSearch.trim() && partitionFilter === ALL_PARTITIONS_FILTER;
  const requestedPage = requestServerPage
    ? { limit: ACTIVE_JOBS_PAGE_SIZE, offset: activePageIndex * ACTIVE_JOBS_PAGE_SIZE }
    : undefined;
  const userId = user?.id ?? 0;
  const queryScope = { userId, isAdmin };

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search), JOB_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timeout);
  }, [search]);

  const tasksQuery = useQuery({
    ...jobsTaskListQueryOptions(queryScope, taskStatusFilter, requestedPage),
    queryFn: async () => {
      try {
        return await listTasks(taskStatusFilter, requestedPage);
      } catch (error) {
        if (requestServerPage && error instanceof ApiError && error.code === "TASK_LIST_PAGINATION_UNAVAILABLE") {
          return listTasks(taskStatusFilter);
        }
        throw error;
      }
    },
    enabled: !!user,
  });
  const queueInfoQuery = useQuery({
    ...jobsQueueInfoQueryOptions(queryScope),
    enabled: !!user && isAdmin,
  });

  const tasks = useMemo(() => tasksQuery.data?.tasks ?? [], [tasksQuery.data?.tasks]);
  const serverPagination = tasksQuery.data?.pagination;
  const isServerPaginated = requestServerPage && !!serverPagination;
  const partitionOptions = useMemo(() => {
    if (isServerPaginated && serverPagination) return serverPagination.available_partitions;
    return Array.from(new Set(tasks.map((task) => str(task.details?.partition)).filter(Boolean))).sort();
  }, [isServerPaginated, serverPagination, tasks]);
  const filteredTasks = useMemo(() => {
    const q = debouncedSearch.trim().toLowerCase();
    return tasks.filter((task) => {
      const filename = str(task.details?.metadata?.filename);
      const fileId = str(task.details?.file_id);
      const partition = str(task.details?.partition);
      const matchesSearch =
        !q ||
        [task.task_id, task.state, filename, fileId, partition, isAdmin ? task.error_summary : ""].some((value) =>
          str(value).toLowerCase().includes(q),
        );
      const matchesPartition = partitionFilter === ALL_PARTITIONS_FILTER || partition === partitionFilter;
      return matchesSearch && matchesPartition;
    });
  }, [tasks, debouncedSearch, partitionFilter, isAdmin]);
  const selectedActiveTasks = useMemo(
    () => filteredTasks.filter((task) => rowSelection[task.task_id] && isActiveState(task.state)),
    [filteredTasks, rowSelection],
  );

  const bulkCancelMutation = useMutation({
    mutationFn: async (taskIds: string[]) => {
      const latestTasks = await listTasks(taskStatusFilter);
      const selectedIds = new Set(taskIds);
      const activeTaskIds = latestTasks.tasks
        .filter((task) => selectedIds.has(task.task_id) && isActiveState(task.state))
        .map((task) => task.task_id);
      const results = await Promise.allSettled(activeTaskIds.map((taskId) => cancelTask(taskId)));
      const ok = results.filter((result) => result.status === "fulfilled").length;
      return { ok, failed: results.length - ok, skipped: taskIds.length - activeTaskIds.length };
    },
    onSuccess: ({ ok, failed, skipped }) => {
      if (ok) toast.success(`${ok} task(s) cancelled`);
      if (failed) toast.error(`${failed} task(s) failed to cancel`);
      if (skipped) toast.error(`${skipped} task(s) were no longer active`);
      setRowSelection({});
      void invalidateJobsQueries(queryClient);
    },
    onError: (error: Error) => toast.error(`Bulk cancel failed: ${error.message}`),
  });

  const exportJobs = async () => {
    try {
      const exportRows = isServerPaginated
        ? (await listTasks(taskStatusFilter)).tasks
        : filteredTasks;
      downloadCsv(
        "openrag-jobs.csv",
        [
          { header: "task_id", value: (task) => task.task_id },
          { header: "state", value: (task) => task.state },
          { header: "outcome", value: (task) => task.outcome },
          { header: "degraded_stages", value: (task) => task.details?.degraded_stages?.join(",") },
          { header: "filename", value: (task) => str(task.details?.metadata?.filename) },
          { header: "file_id", value: (task) => str(task.details?.file_id) },
          { header: "partition", value: (task) => str(task.details?.partition) },
          { header: "created_at", value: (task) => task.created_at },
          { header: "duration_ms", value: (task) => task.duration_ms },
          ...(isAdmin
            ? [{ header: "failure_reason", value: (task: TaskListItem) => task.error_summary }]
            : []),
        ],
        exportRows,
      );
    } catch (error) {
      toast.error(`CSV export failed: ${error instanceof Error ? error.message : "Unknown error"}`);
    }
  };

  const handleStatusTabChange = (value: string) => {
    setStatusTab(value);
    setSearch("");
    setDebouncedSearch("");
    setPartitionFilter(ALL_PARTITIONS_FILTER);
    setActivePageIndex(0);
    setRowSelection({});
  };

  return (
    <div>
      <PageHeader title="Jobs" description={isAdmin ? "Monitor indexing tasks" : "Monitor your indexing tasks"} />

      <Tabs value={statusTab} onValueChange={handleStatusTabChange}>
        <div className="mb-4 mt-0 flex flex-wrap items-center gap-2">
          <div className="relative max-w-sm">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search jobs..."
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setActivePageIndex(0);
                setRowSelection({});
              }}
              className="pl-9"
            />
          </div>
          <Select
            value={partitionFilter}
            onValueChange={(value) => {
              setPartitionFilter(value);
              setActivePageIndex(0);
              setRowSelection({});
            }}
          >
            <SelectTrigger className="w-[180px]" aria-label="Filter jobs by partition">
              <SelectValue placeholder="All partitions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_PARTITIONS_FILTER}>All partitions</SelectItem>
              {partitionOptions.map((partition) => (
                <SelectItem key={partition} value={partition}>
                  {partition}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            <p className="text-sm text-muted-foreground">
              {tasksQuery.isLoading
                ? "Loading jobs…"
                : tasksQuery.isLoadingError
                  ? "Jobs unavailable"
                  : `${isServerPaginated ? serverPagination.total : filteredTasks.length} job${(isServerPaginated ? serverPagination.total : filteredTasks.length) === 1 ? "" : "s"}`}
            </p>
            {selectedActiveTasks.length > 0 && (
              <>
                <ConfirmDialog
                  title="Cancel selected tasks?"
                  description={`Send a cancellation signal for ${selectedActiveTasks.length} active task(s)? Already-completed work is not rolled back.`}
                  onConfirm={() => bulkCancelMutation.mutate(selectedActiveTasks.map((task) => task.task_id))}
                >
                  <Button variant="outline" size="sm" disabled={bulkCancelMutation.isPending}>
                    <Ban className="h-4 w-4" />
                    Cancel selected
                  </Button>
                </ConfirmDialog>
                <p className="text-sm text-muted-foreground">
                  {selectedActiveTasks.length} selected
                </p>
              </>
            )}
            {isAdmin && (
              <QueuePressureSummary
                queueInfo={queueInfoQuery.data}
                isLoading={queueInfoQuery.isLoading}
                isLoadingError={queueInfoQuery.isLoadingError}
                isRefetchError={queueInfoQuery.isRefetchError}
                isFetching={queueInfoQuery.isFetching}
                onRetry={() => void queueInfoQuery.refetch()}
              />
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => void exportJobs()}
              disabled={!filteredTasks.length}
              title="Export filtered jobs"
            >
              <Download className="h-4 w-4" />
              Export CSV
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              onClick={() => {
                setManualRefreshing(true);
                const refreshes: Promise<unknown>[] = [tasksQuery.refetch()];
                if (isAdmin) refreshes.push(queueInfoQuery.refetch());
                Promise.allSettled(refreshes).finally(() => setManualRefreshing(false));
              }}
              disabled={manualRefreshing}
              aria-label="Refresh jobs"
              title="Refresh jobs"
            >
              <RefreshCw className={manualRefreshing ? "animate-spin" : ""} />
            </Button>
          </div>
        </div>

        <TabsList>
          {STATUS_TABS.map((tab) => (
            <TabsTrigger key={tab} value={tab}>
              {tab.charAt(0) + tab.slice(1).toLowerCase()}
            </TabsTrigger>
          ))}
        </TabsList>

        {STATUS_TABS.map((tab) => (
          <TabsContent key={tab} value={tab}>
            {tasksQuery.isLoading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">Loading tasks...</div>
            ) : tasksQuery.isLoadingError ? (
              <QueryErrorNotice
                title="Jobs could not be loaded"
                message="The job list is unavailable. Try again to reload it."
                isRetrying={tasksQuery.isFetching}
                onRetry={() => void tasksQuery.refetch()}
              />
            ) : (
              <>
                {tasksQuery.isRefetchError && (
                  <div className="mb-3">
                    <QueryErrorNotice
                      title="Jobs could not be refreshed"
                      message="Showing previously loaded jobs. They may be out of date."
                      isRetrying={tasksQuery.isFetching}
                      onRetry={() => void tasksQuery.refetch()}
                      stale
                    />
                  </div>
                )}
                {/* Remounting resets pagination for a new tab/search context; sort state resets with it. */}
                <DataTable
                  key={`${statusTab}:${debouncedSearch}:${partitionFilter}`}
                  pageSize={statusTab === "ACTIVE" ? ACTIVE_JOBS_PAGE_SIZE : undefined}
                  columns={statusTab === "ACTIVE"
                    ? (isAdmin ? adminActiveColumns : activeColumns)
                    : (isAdmin ? adminColumns : columns)}
                  data={filteredTasks}
                  disableSorting={statusTab === "ACTIVE"}
                  manualPage={
                    isServerPaginated && serverPagination
                      ? {
                          pageIndex: activePageIndex,
                          pageCount: Math.max(1, Math.ceil(serverPagination.total / serverPagination.limit)),
                          onPageIndexChange: (pageIndex) => {
                            setActivePageIndex(pageIndex);
                            setRowSelection({});
                          },
                        }
                      : undefined
                  }
                  enableSelection
                  canSelectRow={(task) => isActiveState(task.state)}
                  getRowId={(task) => task.task_id}
                  rowSelection={rowSelection}
                  onRowSelectionChange={setRowSelection}
                />
              </>
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
