import { useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban } from "lucide-react";
import { toast } from "sonner";

import { StatusBadge } from "@/components/shared/status-badge";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  getTaskStatus,
  getTaskError,
  cancelTask,
  isActiveState,
  isTerminalState,
} from "@/lib/api/jobs";

const str = (v: unknown) => (v == null ? "" : String(v));

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  // OpenRag has no task SSE — poll status until it reaches a terminal state.
  const taskQuery = useQuery({
    queryKey: ["task", id],
    queryFn: () => getTaskStatus(id!),
    enabled: !!id,
    refetchInterval: (query) => {
      const state = query.state.data?.task_state;
      return state && isTerminalState(state) ? false : 3000;
    },
  });

  const state = taskQuery.data?.task_state;
  const active = !!state && isActiveState(state);
  const failed = state === "FAILED";

  const errorQuery = useQuery({
    queryKey: ["task-error", id],
    queryFn: () => getTaskError(id!),
    enabled: !!id && failed,
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelTask(id!),
    onSuccess: (res) => {
      toast.success(res.message ?? "Cancellation signal sent");
      queryClient.invalidateQueries({ queryKey: ["task", id] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err: Error) => toast.error(`Failed to cancel: ${err.message}`),
  });

  if (taskQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        Loading task...
      </div>
    );
  }

  if (taskQuery.isError) {
    return (
      <div className="flex items-center justify-center py-12 text-destructive">
        Failed to load task: {(taskQuery.error as Error).message}
      </div>
    );
  }

  const task = taskQuery.data;
  if (!task) return null;

  const details = task.details;
  const filename = str(details?.metadata?.filename) || str(details?.file_id) || "—";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/jobs">
            <ArrowLeft className="h-4 w-4" />
            Back to Jobs
          </Link>
        </Button>
      </div>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Task {task.task_id.slice(0, 8)}
          </h1>
          <p className="text-muted-foreground mt-1 font-mono text-sm">
            {task.task_id}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={task.task_state} />
          {active && (
            <span className="text-sm text-muted-foreground animate-pulse">
              Auto-refreshing…
            </span>
          )}
          {active && (
            <ConfirmDialog
              title="Cancel Task"
              description={`Send a cancellation signal for "${filename}"? Already-completed work is not rolled back.`}
              onConfirm={() => cancelMutation.mutate()}
            >
              <Button variant="destructive" size="sm" disabled={cancelMutation.isPending}>
                <Ban className="h-4 w-4" />
                {cancelMutation.isPending ? "Cancelling…" : "Cancel Task"}
              </Button>
            </ConfirmDialog>
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Task Details</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3 text-sm">
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Task ID</dt>
              <dd className="font-mono font-medium">{task.task_id}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">State</dt>
              <dd>
                <StatusBadge status={task.task_state} />
              </dd>
            </div>
            <Separator className="sm:col-span-2" />
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">File</dt>
              <dd className="font-medium">
                {details?.file_id && details?.partition ? (
                  <Link
                    to={`/documents/${encodeURIComponent(str(details.partition))}/${encodeURIComponent(str(details.file_id))}`}
                    className="text-primary hover:underline"
                  >
                    {filename}
                  </Link>
                ) : (
                  filename
                )}
              </dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Partition</dt>
              <dd className="font-medium">{str(details?.partition) || "—"}</dd>
            </div>
            <Separator className="sm:col-span-2" />
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">File ID</dt>
              <dd className="font-mono font-medium">{str(details?.file_id) || "—"}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">User ID</dt>
              <dd className="font-medium">{str(details?.user_id) || "—"}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {failed && (
        <Card className="border-destructive/40">
          <CardHeader>
            <CardTitle className="text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>
            {errorQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading error…</p>
            ) : errorQuery.data?.traceback?.length ? (
              <pre className="overflow-x-auto rounded-md bg-muted p-4 text-xs leading-relaxed text-destructive whitespace-pre-wrap">
                {errorQuery.data.traceback.join("\n")}
              </pre>
            ) : (
              <p className="text-sm text-muted-foreground">No error details available.</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
