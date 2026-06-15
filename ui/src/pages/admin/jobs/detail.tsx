import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, useCallback } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowLeft } from "lucide-react";

import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { formatDate, formatDuration } from "@/lib/utils";
import { getJob, streamJobEvents } from "@/lib/api/jobs";
import type {
  JobDocumentStatus,
  StageTiming,
  ChunkStoredEvent,
} from "@/lib/api/jobs";

function formatSeconds(val: number | undefined | null): string {
  if (val == null) return "--";
  if (val < 0.01) return "<0.01s";
  return `${val.toFixed(1)}s`;
}

function totalTiming(t: StageTiming | null): number | null {
  if (!t) return null;
  let sum = 0;
  let hasAny = false;
  for (const v of Object.values(t)) {
    if (v != null) {
      sum += v;
      hasAny = true;
    }
  }
  return hasAny ? sum : null;
}

interface DocProgress {
  filename: string;
  chunks_completed: number;
  timing: StageTiming;
}

const documentColumns: ColumnDef<JobDocumentStatus, unknown>[] = [
  {
    accessorKey: "filename",
    header: "Filename",
    cell: ({ row }) => (
      <Link
        to={`/documents/${row.original.id}`}
        className="text-primary hover:underline font-medium"
      >
        {row.original.filename}
      </Link>
    ),
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "chunk_count",
    header: "Chunks",
  },
  {
    id: "parse",
    header: "Parse",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.parse_s),
  },
  {
    id: "caption",
    header: "Caption",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.caption_s),
  },
  {
    id: "chunk",
    header: "Chunk",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.chunk_s),
  },
  {
    id: "context",
    header: "Context",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.contextualize_s),
  },
  {
    id: "embed",
    header: "Embed",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.embed_s),
  },
  {
    id: "store",
    header: "Store",
    cell: ({ row }) => formatSeconds(row.original.stage_timings?.store_s),
  },
  {
    id: "total",
    header: "Total",
    cell: ({ row }) => formatSeconds(totalTiming(row.original.stage_timings)),
  },
  {
    accessorKey: "error_message",
    header: "Error",
    cell: ({ row }) => {
      const error = row.original.error_message;
      if (!error) return <span className="text-muted-foreground">--</span>;
      return (
        <span className="text-destructive text-sm" title={error}>
          {error.length > 80 ? `${error.slice(0, 80)}...` : error}
        </span>
      );
    },
  },
];

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [liveProgress, setLiveProgress] = useState<Map<string, DocProgress>>(
    new Map()
  );
  const [sseConnected, setSseConnected] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);

  const jobQuery = useQuery({
    queryKey: ["job", id],
    queryFn: () => getJob(id!),
    enabled: !!id,
    refetchInterval: (query) => {
      const status = query.state.data?.status?.toUpperCase();
      if (status === "QUEUED" || status === "RUNNING") {
        return 3000;
      }
      return false;
    },
  });

  const isActive =
    jobQuery.data?.status?.toUpperCase() === "QUEUED" ||
    jobQuery.data?.status?.toUpperCase() === "RUNNING";

  const handleChunkStored = useCallback((event: ChunkStoredEvent) => {
    setLiveProgress((prev) => {
      const next = new Map(prev);
      const existing = next.get(event.doc_id);
      next.set(event.doc_id, {
        filename: event.filename,
        chunks_completed: (existing?.chunks_completed || 0) + 1,
        timing: event.timing,
      });
      return next;
    });
  }, []);

  const handleJobCompleted = useCallback(() => {
    setSseConnected(false);
    jobQuery.refetch();
  }, [jobQuery]);

  const handleSseError = useCallback(() => {
    setSseConnected(false);
  }, []);

  useEffect(() => {
    if (!id || !isActive) return;

    const controller = streamJobEvents(
      id,
      handleChunkStored,
      handleJobCompleted,
      handleSseError,
    );
    controllerRef.current = controller;
    setSseConnected(true);

    return () => {
      controller.abort();
      controllerRef.current = null;
      setSseConnected(false);
    };
  }, [id, isActive, handleChunkStored, handleJobCompleted, handleSseError]);

  if (jobQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        Loading job...
      </div>
    );
  }

  if (jobQuery.isError) {
    return (
      <div className="flex items-center justify-center py-12 text-destructive">
        Failed to load job: {(jobQuery.error as Error).message}
      </div>
    );
  }

  const job = jobQuery.data;
  if (!job) return null;

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
            Job {job.id.slice(0, 8)}...
          </h1>
          <p className="text-muted-foreground mt-1 font-mono text-sm">
            {job.id}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={job.status} />
          {isActive && (
            <span className="text-sm text-muted-foreground animate-pulse">
              {sseConnected ? "Live" : "Auto-refreshing..."}
            </span>
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Job Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3 text-sm">
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Job ID</dt>
              <dd className="font-mono font-medium">{job.id}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Status</dt>
              <dd>
                <StatusBadge status={job.status} />
              </dd>
            </div>
            <Separator className="sm:col-span-2" />
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Partition</dt>
              <dd className="font-medium">{job.partition}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Total Documents</dt>
              <dd className="font-medium">{job.total_documents}</dd>
            </div>
            <Separator className="sm:col-span-2" />
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Created</dt>
              <dd className="font-medium">{formatDate(job.created_at)}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Started</dt>
              <dd className="font-medium">{formatDate(job.started_at)}</dd>
            </div>
            <Separator className="sm:col-span-2" />
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Completed</dt>
              <dd className="font-medium">{formatDate(job.completed_at)}</dd>
            </div>
            <div className="flex justify-between sm:flex-col sm:gap-1">
              <dt className="text-muted-foreground">Duration</dt>
              <dd className="font-medium">
                {formatDuration(job.started_at, job.completed_at)}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {isActive && liveProgress.size > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Live Progress</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {Array.from(liveProgress.entries()).map(([docId, progress]) => (
                <div
                  key={docId}
                  className="flex items-center justify-between text-sm"
                >
                  <span className="font-medium truncate max-w-[300px]">
                    {progress.filename}
                  </span>
                  <span className="text-muted-foreground">
                    {progress.chunks_completed} chunks stored
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Documents ({job.documents.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable columns={documentColumns} data={job.documents} />
        </CardContent>
      </Card>
    </div>
  );
}
