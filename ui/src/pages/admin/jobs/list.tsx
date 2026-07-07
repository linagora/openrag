import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw, Search } from "lucide-react";
import { usePermissions } from "@/lib/permissions";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { listTasks, type TaskListItem } from "@/lib/api/jobs";

// OpenRag exposes per-file indexing tasks (TaskStateManager), not batch "jobs".
const STATUS_TABS = ["ALL", "ACTIVE", "COMPLETED", "FAILED", "CANCELLED"] as const;

const str = (v: unknown) => (v == null ? "" : String(v));

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
  const [statusTab, setStatusTab] = useState<string>("ALL");
  const [search, setSearch] = useState("");
  const [manualRefreshing, setManualRefreshing] = useState(false);

  const tasksQuery = useQuery({
    queryKey: ["tasks", statusTab],
    queryFn: () => listTasks(statusTab === "ALL" ? undefined : statusTab === "ACTIVE" ? "active" : statusTab),
    refetchInterval: 2000, // poll — OpenRag has no task SSE (scale-correct per roadmap)
  });

  const tasks = useMemo(() => tasksQuery.data?.tasks ?? [], [tasksQuery.data?.tasks]);
  const filteredTasks = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter((task) => {
      const filename = str(task.details?.metadata?.filename);
      const fileId = str(task.details?.file_id);
      const partition = str(task.details?.partition);
      return [task.task_id, task.state, filename, fileId, partition].some((value) =>
        str(value).toLowerCase().includes(q),
      );
    });
  }, [tasks, search]);

  return (
    <div>
      <PageHeader title="Jobs" description={isAdmin ? "Monitor indexing tasks" : "Monitor your indexing tasks"} />

      <Tabs value={statusTab} onValueChange={setStatusTab}>
        <div className="mb-4 mt-0 flex items-center gap-2">
          <div className="relative max-w-sm">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search jobs..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9"
            />
          </div>
          <div className="ml-auto flex items-center gap-2">
            <p className="text-sm text-muted-foreground">
              {filteredTasks.length} job{filteredTasks.length === 1 ? "" : "s"}
            </p>
            <Button
              variant="outline"
              size="icon-sm"
              onClick={() => {
                setManualRefreshing(true);
                tasksQuery.refetch().finally(() => setManualRefreshing(false));
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
              <DataTable key={`${statusTab}:${search}`} columns={columns} data={filteredTasks} />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
