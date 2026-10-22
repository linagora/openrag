import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { MouseEvent } from "react";
import { ArrowLeft, Ban, Copy } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/lib/auth";
import { usePermissions } from "@/lib/permissions";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  DegradedCompletionStatus,
  normalizeDegradedStages,
} from "@/components/shared/degraded-stages";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
  getTaskStageProgress,
  cancelTask,
  isActiveState,
  isTerminalState,
} from "@/lib/api/jobs";
import { invalidateJobsQueries } from "@/lib/jobs-queries";
import { copyToClipboard, formatDate } from "@/lib/utils";

const str = (v: unknown) => (v == null ? "" : String(v));

function tracebackSummary(lines: string[]): string {
  return [...lines].reverse().find((line) => line.trim() && !line.trim().startsWith("Traceback"))?.trim() ?? "";
}

function failedStage(details: unknown): string {
  if (!details || typeof details !== "object") return "";
  const record = details as Record<string, unknown>;
  return str(record.failed_stage);
}

function formatStageDuration(durationMs: number | null | undefined): string | null {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 0) return null;
  const seconds = Math.floor(durationMs / 1000);
  if (seconds < 1) return "< 1s";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function historyQualityLabel(quality: string | undefined): string {
  switch (quality) {
    case "complete": return "All observed stage events are saved.";
    case "pending_persistence": return "Recent stage updates may still be saving.";
    case "incomplete": return "Some stage history may be missing.";
    case "truncated": return "Only the most recent stage history is available.";
    case "legacy": return "Stage history was not recorded for this task.";
    case "not_started": return "No pipeline stage has started yet.";
    default: return "Stage history completeness is unknown.";
  }
}

function stageEventText(
  eventStatus: string,
  failureEffect: string | null | undefined,
  isOpenCurrentInvocation: boolean,
  isInterruptedInvocation: boolean,
  isCurrentlyRunning: boolean,
  taskTerminal: boolean,
): string {
  if (eventStatus === "started") {
    if (isInterruptedInvocation) return "Interrupted — worker stopped before a stage end was observed";
    if (isOpenCurrentInvocation && taskTerminal) return "Last observed start — no stage end was recorded before the task ended";
    if (isOpenCurrentInvocation && isCurrentlyRunning) return "Running";
    if (isOpenCurrentInvocation) return "Last observed start — worker activity is not confirmed";
    return "Started";
  }
  if (eventStatus === "completed") return "Completed";
  if (eventStatus === "cancelled") return "Cancelled";
  if (failureEffect === "continued_degraded") return "Failed; indexing continued without this enrichment";
  return "Failed";
}

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { user } = useAuth();
  const { isAdmin } = usePermissions();
  const queryClient = useQueryClient();
  const userId = user?.id ?? 0;
  const authorizationScope = isAdmin ? "admin" : "user";
  const historyIdentity = `${id ?? ""}:${userId}:${authorizationScope}`;
  const [historyState, setHistoryState] = useState<{
    identity: string;
    cursors: Array<{ attempt: number; sequence: number } | null>;
  }>(() => ({ identity: historyIdentity, cursors: [null] }));
  const historyCursors = historyState.identity === historyIdentity ? historyState.cursors : [null];
  const updateHistoryCursors = (
    update: (current: Array<{ attempt: number; sequence: number } | null>) => Array<{
      attempt: number;
      sequence: number;
    } | null>,
  ) => {
    setHistoryState((current) => ({
      identity: historyIdentity,
      cursors: update(current.identity === historyIdentity ? current.cursors : [null]),
    }));
  };
  const historyCursor = historyCursors[historyCursors.length - 1] ?? null;

  // OpenRag has no task SSE — poll status until it reaches a terminal state.
  const taskQuery = useQuery({
    queryKey: ["task", id, userId, authorizationScope],
    queryFn: () => getTaskStatus(id!),
    enabled: !!id && !!user,
    refetchInterval: (query) => {
      const state = query.state.data?.task_state;
      return state && isTerminalState(state) ? false : 3000;
    },
  });

  const stageQuery = useQuery({
    queryKey: [
      "task-stage-progress",
      id,
      userId,
      authorizationScope,
      historyCursor?.attempt ?? null,
      historyCursor?.sequence ?? null,
    ],
    queryFn: () => getTaskStageProgress(id!, historyCursor, 50),
    enabled: !!id && !!user,
    refetchInterval: historyCursors.length === 1 && isActiveState(taskQuery.data?.task_state ?? "") ? 3000 : false,
  });

  const state = taskQuery.data?.task_state;
  const active = !!state && isActiveState(state);
  const failed = state === "FAILED";

  const errorQuery = useQuery({
    queryKey: ["task-error", id, userId, authorizationScope],
    queryFn: () => getTaskError(id!),
    enabled: !!id && !!user && failed,
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelTask(id!),
    onSuccess: (res) => {
      toast.success(res.message ?? "Cancellation signal sent");
      queryClient.invalidateQueries({ queryKey: ["task", id] });
      void invalidateJobsQueries(queryClient);
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
  const degradedStages = normalizeDegradedStages(details?.degraded_stages);
  const degraded = task.task_state === "COMPLETED" && degradedStages.length > 0;
  const filename = str(details?.metadata?.filename) || str(details?.file_id) || "—";
  const traceback = errorQuery.data?.traceback ?? [];
  const reason =
    errorQuery.data?.reason?.trim() ||
    errorQuery.data?.summary?.trim() ||
    tracebackSummary(traceback);
  const stage = failedStage(details);
  const stageEvents = stageQuery.data?.events ?? [];
  const taskTerminal = isTerminalState(task.task_state);
  const currentInvocationId = stageQuery.data?.current_invocation_id;
  const interruptedInvocationId = stageQuery.data?.interrupted_invocation_id;
  const nextStageCursor = stageQuery.data?.next_cursor;
  const diagnostics = [
    `Task ID: ${task.task_id}`,
    `State: ${task.task_state}`,
    `File: ${filename}`,
    `Partition: ${str(details?.partition) || "—"}`,
    stage ? `Failed stage: ${stage}` : null,
    reason ? `Reason: ${reason}` : null,
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
          {degraded ? (
            <DegradedCompletionStatus stages={degradedStages} />
          ) : (
            <StatusBadge status={task.task_state} />
          )}
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
                {degraded ? (
                  <DegradedCompletionStatus stages={degradedStages} />
                ) : (
                  <StatusBadge status={task.task_state} />
                )}
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

      <Card>
        <CardHeader>
          <CardTitle>Indexing stages</CardTitle>
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {stageQuery.data ? historyQualityLabel(stageQuery.data.history_quality) : "Loading stage history…"}
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {stageQuery.isError ? (
            <p className="text-sm text-destructive">Stage history is currently unavailable.</p>
          ) : stageEvents.length ? (
            <ol className="space-y-3 border-l pl-4">
              {stageEvents.map((event) => {
                const isOpenCurrentInvocation =
                  event.status === "started" && !!currentInvocationId && event.invocation_id === currentInvocationId;
                  const isInterruptedInvocation =
                    event.status === "started" &&
                    !!interruptedInvocationId &&
                    event.invocation_id === interruptedInvocationId;
                const duration = formatStageDuration(event.duration_ms);
                return (
                  <li key={`${event.attempt}:${event.sequence}`} className="relative space-y-1">
                    <span className="absolute -left-[1.34rem] top-1.5 h-2 w-2 rounded-full bg-primary" />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{event.stage_id.replace(/_/g, " ")}</span>
                      <Badge variant={event.status === "failed" ? "destructive" : "outline"}>
                        {stageEventText(
                          event.status,
                          event.failure_effect,
                          isOpenCurrentInvocation,
                          isInterruptedInvocation,
                          !!stageQuery.data?.is_currently_running,
                          taskTerminal,
                        )}
                      </Badge>
                      {duration && <span className="text-sm text-muted-foreground">{duration}</span>}
                    </div>
                    <time className="block text-xs text-muted-foreground" dateTime={event.occurred_at}>
                      {formatDate(event.occurred_at)}
                    </time>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">
              {stageQuery.data?.history_quality === "not_started" ? "No pipeline stage has started yet." : "No stage events are available."}
            </p>
          )}
          <div className="flex gap-2">
            {historyCursors.length > 1 && (
              <Button variant="outline" size="sm" onClick={() => updateHistoryCursors((current) => current.slice(0, -1))}>
                Newer stages
              </Button>
            )}
            {stageQuery.data?.has_more && nextStageCursor && (
              <Button variant="outline" size="sm" onClick={() => updateHistoryCursors((current) => [...current, nextStageCursor])}>
                Older stages
              </Button>
            )}
          </div>
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
                    <dd className="font-medium break-words">{reason || "Task failed"}</dd>
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
