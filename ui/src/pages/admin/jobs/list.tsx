import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useAuth } from "@/lib/auth";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { formatDate, formatDuration } from "@/lib/utils";
import { listJobs } from "@/lib/api/jobs";
import type { JobResponse } from "@/lib/api/jobs";

const STATUS_TABS = ["ALL", "QUEUED", "RUNNING", "SUCCESS", "FAILED"] as const;

const columns: ColumnDef<JobResponse, unknown>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ row }) => (
      <Link
        to={`/jobs/${row.original.id}`}
        className="text-primary hover:underline font-mono text-sm"
      >
        {row.original.id.slice(0, 8)}
      </Link>
    ),
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "partition",
    header: "Partition",
  },
  {
    accessorKey: "total_documents",
    header: "Documents",
  },
  {
    accessorKey: "created_at",
    header: "Created",
    cell: ({ row }) => formatDate(row.original.created_at),
  },
  {
    id: "duration",
    header: "Duration",
    cell: ({ row }) =>
      formatDuration(row.original.started_at, row.original.completed_at),
  },
];

export default function JobListPage() {
  const { isAdmin } = useAuth();
  const [statusTab, setStatusTab] = useState<string>("ALL");

  const jobsQuery = useQuery({
    queryKey: ["jobs", statusTab],
    queryFn: () =>
      listJobs({
        status: statusTab === "ALL" ? undefined : statusTab,
        limit: 200,
      }),
    refetchInterval: 5000,
  });

  const jobs = jobsQuery.data?.jobs ?? [];

  return (
    <div>
      <PageHeader title="Jobs" description={isAdmin ? "Monitor indexing job progress" : "Monitor your indexing jobs"} />

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
            {jobsQuery.isLoading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                Loading jobs...
              </div>
            ) : jobsQuery.isError ? (
              <div className="flex items-center justify-center py-12 text-destructive">
                Failed to load jobs: {(jobsQuery.error as Error).message}
              </div>
            ) : (
              <DataTable columns={columns} data={jobs} />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
