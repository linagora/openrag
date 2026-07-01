import type { ComponentType } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Database,
  FileText,
  Users,
  Activity,
  Plus,
  Upload,
  ArrowRight,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { QuotaUsageMeter } from "@/components/shared/quota-usage-meter";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { listPartitions } from "@/lib/api/partitions";
import { listUsers } from "@/lib/api/users";
import { listTasks, isActiveState, type TaskListItem } from "@/lib/api/jobs";
import { healthCheck, getVersion, listActors } from "@/lib/api/system";
import { usePermissions } from "@/lib/permissions";

const str = (v: unknown) => (v == null ? "" : String(v));

function StatCard({
  title,
  value,
  icon: Icon,
  isLoading,
}: {
  title: string;
  value: string | number;
  icon: ComponentType<{ className?: string }>;
  isLoading: boolean;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        {isLoading ? <Skeleton className="h-8 w-20" /> : <div className="text-2xl font-bold">{value}</div>}
      </CardContent>
    </Card>
  );
}

// OpenRag exposes per-file indexing tasks (not batch jobs), with no timestamp
// or doc-count on the list row — so the table shows task/file/partition/state.
function RecentTasks({ isLoading, tasks }: { isLoading: boolean; tasks: TaskListItem[] }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-base">Recent Jobs</CardTitle>
        <Button variant="ghost" size="sm" asChild>
          <Link to="/jobs">
            View all
            <ArrowRight className="h-4 w-4 ml-1" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : tasks.length > 0 ? (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead>File</TableHead>
                  <TableHead>Partition</TableHead>
                  <TableHead>State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.map((task) => (
                  <TableRow key={task.task_id}>
                    <TableCell>
                      <Link
                        to={`/jobs/${task.task_id}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {task.task_id.slice(0, 8)}
                      </Link>
                    </TableCell>
                    <TableCell className="max-w-[220px] truncate">
                      {str(task.details?.metadata?.filename) || str(task.details?.file_id) || "—"}
                    </TableCell>
                    <TableCell>{str(task.details?.partition) || "—"}</TableCell>
                    <TableCell>
                      <StatusBadge status={task.state} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-6">No jobs found.</p>
        )}
      </CardContent>
    </Card>
  );
}

export default function OverviewPage() {
  const { canManageUsers, canViewSystem, canCreatePartition, canWrite } = usePermissions();

  // `GET /partition/` and `/queue/tasks` are membership-scoped server-side, so the
  // same queries serve admins and partition members. Platform-only data is gated.
  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const tasksQuery = useQuery({
    queryKey: ["tasks", "overview"],
    queryFn: () => listTasks(),
    refetchInterval: 5000,
  });
  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: listUsers,
    enabled: canManageUsers,
  });
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: healthCheck,
    enabled: canViewSystem,
    refetchInterval: 30000,
  });
  const versionQuery = useQuery({ queryKey: ["version"], queryFn: getVersion, enabled: canViewSystem });
  const actorsQuery = useQuery({
    queryKey: ["actors", "overview"],
    queryFn: listActors,
    enabled: canViewSystem,
    refetchInterval: 30000,
  });

  const tasks = tasksQuery.data?.tasks ?? [];
  const partitions = partitionsQuery.data?.partitions ?? [];
  const actors = actorsQuery.data?.actors ?? [];
  const partitionCount = partitions.length;
  const documentTotal = partitions.reduce((sum, p) => sum + p.document_count, 0);
  const userCount = usersQuery.data?.users.length ?? 0;
  const activeTasks = tasks.filter((t) => isActiveState(t.state)).length;
  const actorsAlive = actors.filter((a) => a.state.toUpperCase() === "ALIVE").length;

  // Personal file-quota usage (from /users/info). file_quota is the *effective*
  // quota the backend already resolved: -1 / null = unlimited.
  const me = useAuth().user;
  const quotaIndexed = me?.file_count ?? 0;
  const quotaEff: number | "unlimited" =
    me?.file_quota == null || me.file_quota < 0 ? "unlimited" : me.file_quota;
  const writablePartition = partitions.find((partition) => canWrite(partition.role));
  const uploadQuickAction = partitionsQuery.isLoading
    ? null
    : writablePartition
      ? {
          href: `/documents?partition=${encodeURIComponent(writablePartition.name)}`,
          title: "Upload Documents",
          description: "Add documents to a writable partition",
          icon: Upload,
        }
      : canCreatePartition
        ? {
            href: "/partitions?create=1",
            title: "Create Partition to Upload",
            description: "Create a partition first, then add documents",
            icon: Plus,
          }
        : null;
  const UploadQuickActionIcon = uploadQuickAction?.icon;

  return (
    <div>
      <PageHeader title="Overview" description="Dashboard and quick actions" />

      {/* Stat Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-8">
        <StatCard
          title="Partitions"
          value={partitionCount}
          icon={Database}
          isLoading={partitionsQuery.isLoading}
        />
        <StatCard
          title="Documents"
          value={documentTotal}
          icon={FileText}
          isLoading={partitionsQuery.isLoading}
        />
        {canManageUsers && (
          <StatCard title="Users" value={userCount} icon={Users} isLoading={usersQuery.isLoading} />
        )}
        <StatCard
          title="Active Jobs"
          value={activeTasks}
          icon={Activity}
          isLoading={tasksQuery.isLoading}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2 mb-8">
        {/* System status — platform operators only */}
        {canViewSystem && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">System status</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-3">
                <div className="flex items-center justify-between pb-2 border-b">
                  <span className="text-sm font-medium">API</span>
                  {healthQuery.isSuccess ? (
                    <Badge
                      variant="outline"
                      className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                    >
                      <CheckCircle2 className="h-3 w-3 mr-1" />
                      Up
                    </Badge>
                  ) : (
                    <Badge
                      variant="outline"
                      className="bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                    >
                      <XCircle className="h-3 w-3 mr-1" />
                      Down
                    </Badge>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm">Version</span>
                  <span className="text-sm font-mono text-muted-foreground">
                    {versionQuery.data?.version ?? "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm">Ray actors</span>
                  <span className="text-sm font-mono text-muted-foreground">
                    {actorsQuery.isLoading ? "—" : `${actorsAlive}/${actors.length} alive`}
                  </span>
                </div>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Your file quota — regular users only; occupies the System-status slot
            so the dashboard layout stays consistent with the admin view. */}
        {me && !me.is_admin && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Your file quota</CardTitle>
            </CardHeader>
            <CardContent>
              <QuotaUsageMeter fileCount={quotaIndexed} quota={quotaEff} />
            </CardContent>
          </Card>
        )}

        {/* Quick Actions */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Quick Actions</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3">
              {canCreatePartition && (
                <Button variant="outline" className="justify-start h-auto py-3" asChild>
                  <Link to="/partitions?create=1">
                    <Plus className="h-4 w-4 mr-2" />
                    <div className="text-left">
                      <div className="font-medium">Create Partition</div>
                      <div className="text-xs text-muted-foreground">
                        Set up a new document partition
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 ml-auto" />
                  </Link>
                </Button>
              )}
              {uploadQuickAction && (
                <Button variant="outline" className="justify-start h-auto py-3" asChild>
                  <Link to={uploadQuickAction.href}>
                    {UploadQuickActionIcon && <UploadQuickActionIcon className="h-4 w-4 mr-2" />}
                    <div className="text-left">
                      <div className="font-medium">{uploadQuickAction.title}</div>
                      <div className="text-xs text-muted-foreground">
                        {uploadQuickAction.description}
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 ml-auto" />
                  </Link>
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <RecentTasks isLoading={tasksQuery.isLoading} tasks={tasks.slice(0, 5)} />
    </div>
  );
}
