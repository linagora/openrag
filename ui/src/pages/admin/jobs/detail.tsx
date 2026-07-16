import { useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { MouseEvent } from "react";
import { ArrowLeft, Ban, Copy } from "lucide-react";
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
import { copyToClipboard } from "@/lib/utils";

const str = (v: unknown) => (v == null ? "" : String(v));

function errorSummary(lines: string[]): string {
  return [...lines].reverse().find((line) => line.trim() && !line.trim().startsWith("Traceback"))?.trim() ?? "";
}

function failedStage(details: unknown): string {
  if (!details || typeof details !== "object") return "";
  const record = details as Record<string, unknown>;
  return str(record.failed_stage);
}

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
  const traceback = errorQuery.data?.traceback ?? [];
  const summary = errorSummary(traceback);
  const stage = failedStage(details);
  const diagnostics = [
    `Task ID: ${task.task_id}`,
    `State: ${task.task_state}`,
    `File: ${filename}`,
    `Partition: ${str(details?.partition) || "—"}`,
    stage ? `Failed stage: ${stage}` : null,
    summary ? `Reason: ${summary}` : null,
    traceback.length ? `\nTraceback:\n${traceback.join("\n")}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  const copyDiagnostics = async (event: MouseEvent<HTMLButtonElement>) => {
    if (!diagnostics) return;
    const ok = await copyToClipboard(diagnostics, event.currentTarget);
    toast[ok ? "success" : "error"](
      ok ? "Diagnostics copied to clipboard" : "Couldn't copy diagnostics",
    );
  };

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
          <CardHeader className="flex flex-row items-center justify-between gap-4">
            <CardTitle className="text-destructive">Error</CardTitle>
            <Button
              variant="outline"
              size="sm"
              onClick={copyDiagnostics}
              disabled={!diagnostics || errorQuery.isLoading}
            >
              <Copy className="h-4 w-4" />
              Copy diagnostics
            </Button>
          </CardHeader>
          <CardContent className="space-y-4">
            {errorQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading error…</p>
            ) : errorQuery.isError ? (
              <p className="text-sm text-destructive">
                Failed to load error details: {(errorQuery.error as Error).message}
              </p>
            ) : errorQuery.data?.traceback?.length ? (
              <>
                <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm">
                  <dl className="grid gap-2 sm:grid-cols-[8rem_1fr]">
                    <dt className="text-muted-foreground">Reason</dt>
                    <dd className="font-medium break-words">{summary || "Task failed"}</dd>
                    {stage && (
                      <>
                        <dt className="text-muted-foreground">Failed stage</dt>
                        <dd className="font-medium">{stage}</dd>
                      </>
                    )}
                    <dt className="text-muted-foreground">Task ID</dt>
                    <dd className="font-mono text-xs break-all">{task.task_id}</dd>
                    <dt className="text-muted-foreground">File</dt>
                    <dd className="break-words">{filename}</dd>
                  </dl>
                </div>
                <details className="rounded-md border bg-muted/40 p-3" open>
                  <summary className="cursor-pointer text-sm font-medium">Raw traceback</summary>
                  <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-destructive">
                    {traceback.join("\n")}
                  </pre>
                </details>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">No error details available.</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
