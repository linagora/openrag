import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Ban } from "lucide-react";
import { EVAL_POLL_MS, cancelEvalRun, isActiveStatus, listEvalRuns } from "@/lib/api/evaluation";
import { StatusBadge } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { DatasetCard } from "./dataset-card";
import { RunDetail } from "./run-detail";

function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(0)}%`;
}

/**
 * Evaluation tab: upload a dataset, run it, read the numbers.
 *
 * Runs are serialised server-side (one at a time), so the run list drives
 * both the polling cadence and whether a new run can be started.
 */
export function EvaluationTab() {
  const queryClient = useQueryClient();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["eval-runs"],
    queryFn: () => listEvalRuns(),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((run) => isActiveStatus(run.status)) ? EVAL_POLL_MS : false,
  });

  const cancelMut = useMutation({
    mutationFn: (id: string) => cancelEvalRun(id),
    onSuccess: () => {
      toast.success("Cancellation requested");
      queryClient.invalidateQueries({ queryKey: ["eval-runs"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const runs = data ?? [];
  const activeRun = runs.find((run) => isActiveStatus(run.status)) ?? null;
  // Default to the newest run so the tab is not empty after a run finishes.
  const shownRunId = selectedRunId ?? activeRun?.id ?? runs[0]?.id ?? null;

  return (
    <div className="space-y-4">
      <DatasetCard runActive={activeRun !== null} />

      <Card>
        <CardHeader className="flex flex-row items-start justify-between">
          <div>
            <CardTitle>Runs</CardTitle>
            <CardDescription>
              One run at a time, so indexing timings stay comparable between them.
            </CardDescription>
          </div>
          {activeRun && (
            <Button
              size="sm"
              variant="outline"
              disabled={cancelMut.isPending}
              onClick={() => cancelMut.mutate(activeRun.id)}
            >
              <Ban className="mr-2 h-4 w-4" />
              Cancel run
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-24" />
          ) : runs.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">No runs yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Started</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Files/min</TableHead>
                  <TableHead className="text-right">Hit rate</TableHead>
                  <TableHead className="text-right">MRR</TableHead>
                  <TableHead className="text-right">Answers</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow
                    key={run.id}
                    className={`cursor-pointer ${run.id === shownRunId ? "bg-muted/50" : ""}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <TableCell>
                      {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={run.status} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {run.files_per_minute?.toFixed(1) ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{percent(run.hit_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {run.mrr?.toFixed(2) ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {percent(run.answer_pass_rate)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {shownRunId && <RunDetail runId={shownRunId} />}
    </div>
  );
}
