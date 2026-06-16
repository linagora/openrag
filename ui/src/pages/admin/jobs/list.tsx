import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useAuth } from "@/lib/auth";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
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
  const { isAdmin } = useAuth();
  const [statusTab, setStatusTab] = useState<string>("ALL");

  const tasksQuery = useQuery({
    queryKey: ["tasks", statusTab],
    queryFn: () => listTasks(statusTab === "ALL" ? undefined : statusTab === "ACTIVE" ? "active" : statusTab),
    refetchInterval: 5000, // poll — OpenRag has no task SSE (scale-correct per roadmap)
  });

  const tasks = tasksQuery.data?.tasks ?? [];

  return (
    <div>
      <PageHeader title="Jobs" description={isAdmin ? "Monitor indexing tasks" : "Monitor your indexing tasks"} />

      <Tabs value={statusTab} onValueChange={setStatusTab}>
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
              <DataTable columns={columns} data={tasks} />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
