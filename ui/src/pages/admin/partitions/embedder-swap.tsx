import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, XCircle } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { resolveEmbedderName, type ModelEndpointResponse } from "@/lib/api/models";
import { cancelEmbedderSwap, startEmbedderSwap, type EmbedderSwap } from "@/lib/api/partitions";
import { embedderSwapQueryKey } from "./use-embedder-swap";

const files = (n: number) => `${n} file${n === 1 ? "" : "s"}`;

/** How far a running swap has got: the count, and the same count as a bar.
 *
 *  Shared by the partition page and Documents so the two always read alike.
 */
function SwapProgress({ done, total }: { done: number; total: number }) {
  const percent = total > 0 ? Math.floor((done / total) * 100) : 0;
  return (
    <>
      <div className="flex items-baseline justify-between gap-3">
        <span className="tabular-nums">
          {done} of {files(total)}
        </span>
        <span className="text-xs tabular-nums text-muted-foreground">{percent}%</span>
      </div>
      <div
        role="progressbar"
        aria-label="Re-embedding progress"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} />
      </div>
    </>
  );
}

/** Progress of a running swap, or why the last one failed. */
export function EmbedderSwapBanner({
  swap,
  canCancel,
  endpoints,
}: {
  swap: EmbedderSwap | null;
  canCancel: boolean;
  endpoints: ModelEndpointResponse[] | undefined;
}) {
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: (partition: string) => cancelEmbedderSwap(partition),
    onSuccess: (cancelled) => {
      queryClient.setQueryData(embedderSwapQueryKey(cancelled.partition), cancelled);
      toast.success("Re-embedding cancelled");
    },
    onError: (error: Error) => toast.error(`Failed to cancel: ${error.message}`),
  });

  if (swap === null) return null;
  const source = resolveEmbedderName(swap.source_embedder, endpoints);

  if (swap.status === "failed") {
    return (
      <Alert variant="destructive" className="mt-6">
        <XCircle className="h-4 w-4" />
        <AlertTitle>Re-embedding with {swap.target_embedder} failed</AlertTitle>
        <AlertDescription>
          {swap.error && <p className="font-mono text-xs [overflow-wrap:anywhere]">{swap.error}</p>}
          <p>
            The partition still uses {source}. Starting again skips the {files(swap.files_done)} already
            done.
          </p>
        </AlertDescription>
      </Alert>
    );
  }
  if (swap.status !== "running") return null;

  return (
    <Alert className="mt-6">
      <Loader2 className="h-4 w-4 animate-spin" />
      <AlertTitle>Re-embedding with {swap.target_embedder}</AlertTitle>
      <AlertDescription>
        <div className="w-full space-y-2">
          <SwapProgress done={swap.files_done} total={swap.files_total} />
          <p>
            Searches keep using {source} until every file is done. Uploads, metadata changes and copies
            into this partition are paused until then; deleting files still works.
          </p>
          {canCancel && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => cancel.mutate(swap.partition)}
              disabled={cancel.isPending}
            >
              Cancel re-embedding
            </Button>
          )}
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** A running swap, for a page that is not the partition's own — Documents, where
 *  the blocked upload button is the first anyone hears of it.
 *
 *  Read-only on purpose: cancelling is owner-gated and belongs next to the
 *  settings it changes, so this links there instead of repeating the control.
 *  Only a running swap shows; how the last one ended is the owner's business.
 */
export function EmbedderSwapNotice({ swap }: { swap: EmbedderSwap | null }) {
  if (swap === null || swap.status !== "running") return null;
  return (
    <Alert className="mb-4">
      <Loader2 className="h-4 w-4 animate-spin" />
      <AlertTitle>
        Re-embedding {swap.partition} with {swap.target_embedder}
      </AlertTitle>
      <AlertDescription>
        <div className="w-full space-y-2">
          <SwapProgress done={swap.files_done} total={swap.files_total} />
          <p>
            Uploads, metadata changes and copies into this partition are paused until it finishes;
            deleting files still works. Searches keep using the current embedder until then.{" "}
            <Link
              to={`/partitions/${swap.partition}`}
              className="font-medium underline underline-offset-2"
            >
              Partition settings
            </Link>
          </p>
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** Choose the embedder to move a partition to, and start re-embedding.
 *
 *  Mounted only while open, so it starts from `initialTarget` every time.
 */
export function ChangeEmbedderDialog({
  partition,
  currentEmbedder,
  documentCount,
  endpoints,
  initialTarget,
  onClose,
}: {
  partition: string;
  currentEmbedder: string;
  documentCount: number;
  endpoints: ModelEndpointResponse[];
  initialTarget: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState(initialTarget);
  const current = resolveEmbedderName(currentEmbedder, endpoints);
  const start = useMutation({
    mutationFn: () => startEmbedderSwap(partition, target),
    onSuccess: (swap) => {
      queryClient.setQueryData(embedderSwapQueryKey(partition), swap);
      toast.success(`Re-embedding ${files(swap.files_total)} with ${swap.target_embedder}`);
      onClose();
    },
    onError: (error: Error) => toast.error(`Could not start re-embedding: ${error.message}`),
  });

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Change embedder</DialogTitle>
          <DialogDescription>
            Each embedder keeps its own vectors, so the files in{" "}
            <span className="font-medium">{partition}</span> are re-embedded with the new one before
            searches switch to it.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="swap-target">Embedder</Label>
            <Select value={target} onValueChange={setTarget}>
              <SelectTrigger id="swap-target" className="w-full">
                <SelectValue placeholder="Select an embedder" />
              </SelectTrigger>
              <SelectContent>
                {endpoints.map((endpoint) => (
                  <SelectItem key={endpoint.name} value={endpoint.name}>
                    {endpoint.name}
                    {endpoint.model_name && (
                      <span className="text-muted-foreground"> · {endpoint.model_name}</span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted-foreground">
            {target === current ? (
              <li>
                This is the embedder the partition already uses: only files indexed with another model
                or vector field are re-embedded.
              </li>
            ) : (
              <li>
                {files(documentCount)} are re-embedded from their stored text. Nothing is parsed or
                chunked again.
              </li>
            )}
            <li>Searches keep using {current} until every file is done, then switch.</li>
            <li>
              Uploads, metadata changes and copies into this partition are paused while it runs.
              Deleting files still works.
            </li>
          </ul>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" onClick={() => start.mutate()} disabled={!target || start.isPending}>
            {start.isPending ? "Starting..." : "Start re-embedding"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
