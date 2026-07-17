import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef, RowSelectionState } from "@tanstack/react-table";
import { Ban, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";
import { usePermissions } from "@/lib/permissions";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { cancelTask, getQueueInfo, isActiveState, listTasks, type QueueInfo, type TaskListItem } from "@/lib/api/jobs";

// OpenRag exposes per-file indexing tasks (TaskStateManager), not batch "jobs".
const STATUS_TABS = ["ALL", "ACTIVE", "COMPLETED", "FAILED", "CANCELLED"] as const;
const JOBS_REFETCH_INTERVAL_MS = 5000;
const JOB_SEARCH_DEBOUNCE_MS = 250;

const str = (v: unknown) => (v == null ? "" : String(v));

function QueuePressureSummary({
  queueInfo,
  isLoading,
  isError,
}: {
  queueInfo?: QueueInfo;
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Queue: loading...</p>;
  }
  if (isError || !queueInfo) {
    return (
      <p className="text-sm text-muted-foreground" title="Queue information is currently unavailable">
        Queue unavailable
      </p>
    );
  }

  const totalSlots = queueInfo.workers.total_slots;
  const queued = queueInfo.tasks.active_statuses.QUEUED ?? 0;
  const running = Math.max(0, queueInfo.tasks.active - queued);
  const saturated = totalSlots > 0 && queued > 0 && running >= totalSlots;
  const busy = running > 0 || queued > 0;
  const status = saturated ? "Saturated" : busy ? "Busy" : "Idle";

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground" aria-label="Queue pressure">
      <Badge variant="outline" className={saturated ? "text-destructive" : undefined}>
        {status}
      </Badge>
      <span>Queued {queued}</span>
      <span>Running {running}/{totalSlots}</span>
      <span>
        Workers {queueInfo.workers.pool_size} x {queueInfo.workers.max_per_actor}
      </span>
    </div>
  );
}

const columns: ColumnDef<TaskListItem, unknown>[] = [
  {
    accessorKey: "task_id",
    header: "Task",
    cell: ({ row }) => (
      <Link to={`/jobs/${row.original.task_id}`} className="text-primary hover:underline font-mono text-sm">
        {row.original.task_id.slice(0, 8)}
      </Link>
    ),
  },
  {
    accessorKey: "state",
    header: "State",
    cell: ({ row }) => <StatusBadge status={row.original.state} />,
  },
  {
    id: "file",
    header: "File",
    cell: ({ row }) => str(row.original.details?.metadata?.filename) || str(row.original.details?.file_id) || "—",
  },
  {
    id: "partition",
    header: "Partition",
    cell: ({ row }) => str(row.original.details?.partition) || "—",
  },
];

export default function JobListPage() {
  const { isAdmin } = usePermissions();
  const queryClient = useQueryClient();
  const [statusTab, setStatusTab] = useState<string>("ALL");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const taskStatusFilter = statusTab === "ALL" ? undefined : statusTab === "ACTIVE" ? "active" : statusTab;

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search), JOB_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timeout);
  }, [search]);

  const tasksQuery = useQuery({
    queryKey: ["tasks", statusTab],
    queryFn: () => listTasks(taskStatusFilter),
    refetchInterval: JOBS_REFETCH_INTERVAL_MS, // poll — OpenRag has no task SSE
  });
  const queueInfoQuery = useQuery({
    queryKey: ["queue-info"],
    queryFn: getQueueInfo,
    enabled: isAdmin,
    refetchInterval: JOBS_REFETCH_INTERVAL_MS,
  });

  const tasks = useMemo(() => tasksQuery.data?.tasks ?? [], [tasksQuery.data?.tasks]);
  const filteredTasks = useMemo(() => {
    const q = debouncedSearch.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter((task) => {
      const filename = str(task.details?.metadata?.filename);
      const fileId = str(task.details?.file_id);
      const partition = str(task.details?.partition);
      return [task.task_id, task.state, filename, fileId, partition].some((value) =>
        str(value).toLowerCase().includes(q),
      );
    });
  }, [tasks, debouncedSearch]);
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
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (error: Error) => toast.error(`Bulk cancel failed: ${error.message}`),
  });

  const handleStatusTabChange = (value: string) => {
    setStatusTab(value);
    setSearch("");
    setDebouncedSearch("");
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
                setRowSelection({});
              }}
              className="pl-9"
            />
          </div>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            <p className="text-sm text-muted-foreground">
              {filteredTasks.length} job{filteredTasks.length === 1 ? "" : "s"}
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
                isError={queueInfoQuery.isError}
              />
            )}
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
              {tab}
            </TabsTrigger>
          ))}
        </TabsList>

        {STATUS_TABS.map((tab) => (
          <TabsContent key={tab} value={tab}>
            {tasksQuery.isLoading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">Loading tasks...</div>
            ) : tasksQuery.isError ? (
              <div className="flex items-center justify-center py-12 text-destructive">
                Failed to load tasks: {(tasksQuery.error as Error).message}
              </div>
            ) : (
              // Remounting resets pagination for a new tab/search context; sort state resets with it.
              <DataTable
                key={`${statusTab}:${debouncedSearch}`}
                columns={columns}
                data={filteredTasks}
                enableSelection
                canSelectRow={(task) => isActiveState(task.state)}
                getRowId={(task) => task.task_id}
                rowSelection={rowSelection}
                onRowSelectionChange={setRowSelection}
              />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
