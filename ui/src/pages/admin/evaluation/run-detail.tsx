import { useQuery } from "@tanstack/react-query";
import { getEvalRun, isActiveStatus, type EvalRun } from "@/lib/api/evaluation";
import { StatusBadge } from "@/components/shared/status-badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const POLL_MS = 3000;

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function score(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(3);
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="space-y-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono text-lg font-medium tabular-nums">{value}</dd>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function RunDetail({ runId }: { runId: string }) {
  const { data: run, isLoading } = useQuery({
    queryKey: ["eval-run", runId],
    queryFn: () => getEvalRun(runId),
    // Poll only while the run can still change.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && isActiveStatus(status) ? POLL_MS : false;
    },
  });

  if (isLoading) return <Skeleton className="h-64" />;
  if (!run) return null;

  return (
    <div className="space-y-4">
      <RunHeader run={run} />
      {run.error && (
        <Alert variant="destructive">
          <AlertDescription className="[overflow-wrap:anywhere]">{run.error}</AlertDescription>
        </Alert>
      )}
      <IndexingPanel run={run} />
      <QualityPanel run={run} />
      <CasesTable run={run} />
    </div>
  );
}

function RunHeader({ run }: { run: EvalRun }) {
  const elapsed =
    run.started_at && run.finished_at
      ? `${Math.round(
          (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000,
        )}s`
      : "—";

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="font-mono text-base">{run.id.slice(0, 12)}</CardTitle>
          <CardDescription>
            Started {run.started_at ? new Date(run.started_at).toLocaleString() : "—"} · took {elapsed}
          </CardDescription>
        </div>
        <StatusBadge status={run.status} />
      </CardHeader>
    </Card>
  );
}

function IndexingPanel({ run }: { run: EvalRun }) {
  const metrics = run.indexing;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Indexing speed</CardTitle>
        <CardDescription>End-to-end ingestion of the corpus into a throwaway partition.</CardDescription>
      </CardHeader>
      <CardContent>
        {!metrics ? (
          <p className="text-sm text-muted-foreground">Not measured yet.</p>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 text-sm sm:grid-cols-4">
              <Stat label="Files / min" value={metrics.files_per_minute.toFixed(1)} />
              <Stat label="MB / s" value={metrics.megabytes_per_second.toFixed(2)} />
              <Stat label="p50 per file" value={`${metrics.p50_seconds.toFixed(1)}s`} />
              <Stat label="p95 per file" value={`${metrics.p95_seconds.toFixed(1)}s`} />
              <Stat label="Wall time" value={`${metrics.wall_seconds.toFixed(1)}s`} />
              <Stat
                label="Files"
                value={`${metrics.files_total}`}
                hint={metrics.files_failed > 0 ? `${metrics.files_failed} failed` : undefined}
              />
              <Stat
                label="Corpus size"
                value={`${(metrics.bytes_total / (1024 * 1024)).toFixed(1)} MB`}
              />
            </dl>
            {Object.keys(metrics.by_extension).length > 1 && (
              <div className="mt-4 flex flex-wrap gap-4 border-t pt-4 text-xs text-muted-foreground">
                {Object.entries(metrics.by_extension).map(([extension, bucket]) => (
                  <span key={extension}>
                    <span className="font-mono">{extension}</span> · {bucket.files} file(s) ·{" "}
                    {bucket.mean_seconds}s avg
                  </span>
                ))}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function QualityPanel({ run }: { run: EvalRun }) {
  const retrieval = run.retrieval;
  const answer = run.answer;

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Retrieval quality</CardTitle>
          <CardDescription>
            {retrieval
              ? `${retrieval.scored_cases} question(s) scored${
                  retrieval.skipped_cases > 0
                    ? `, ${retrieval.skipped_cases} skipped (no expected_file_ids)`
                    : ""
                }`
              : "Not measured yet."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {retrieval && (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 text-sm">
              <Stat label="Hit rate" value={percent(retrieval.hit_rate)} />
              <Stat label="MRR" value={score(retrieval.mrr)} />
              <Stat label="Recall" value={percent(retrieval.recall)} />
              <Stat label="Context relevance" value={score(retrieval.context_relevance)} />
            </dl>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Answer quality</CardTitle>
          <CardDescription>
            {answer ? `${answer.scored_cases} answer(s) graded by the LLM` : "Not measured yet."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {answer && (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 text-sm">
              <Stat label="Pass rate" value={percent(answer.pass_rate)} />
              <Stat label="Factuality" value={score(answer.factuality)} />
              <Stat label="Rubric" value={score(answer.rubric_score)} />
            </dl>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function CasesTable({ run }: { run: EvalRun }) {
  if (run.cases.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Questions</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Question</TableHead>
                <TableHead className="w-20">Hit</TableHead>
                <TableHead className="w-20">RR</TableHead>
                <TableHead className="w-24">Answer</TableHead>
                <TableHead>Grader</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.cases.map((testCase, index) => (
                <TableRow key={`${testCase.query}-${index}`}>
                  <TableCell className="max-w-md">
                    <p className="truncate font-medium" title={testCase.query}>
                      {testCase.query}
                    </p>
                    {testCase.retrieved_file_ids.length > 0 && (
                      <p className="truncate text-xs text-muted-foreground">
                        {testCase.retrieved_file_ids.join(", ")}
                      </p>
                    )}
                  </TableCell>
                  <TableCell>
                    {testCase.hit === null ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <StatusBadge status={testCase.hit ? "SUCCESS" : "FAILED"} />
                    )}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {testCase.reciprocal_rank === null ? "—" : testCase.reciprocal_rank.toFixed(2)}
                  </TableCell>
                  <TableCell>
                    {testCase.answer_passed === null ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <StatusBadge status={testCase.answer_passed ? "SUCCESS" : "FAILED"} />
                    )}
                  </TableCell>
                  <TableCell className="max-w-sm">
                    <p className="truncate text-xs text-muted-foreground" title={testCase.grader_reason ?? ""}>
                      {testCase.grader_reason ?? "—"}
                    </p>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
