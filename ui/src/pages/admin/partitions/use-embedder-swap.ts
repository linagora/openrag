import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { getEmbedderSwap, type EmbedderSwap } from "@/lib/api/partitions";

export const embedderSwapQueryKey = (partition: string) => ["embedder-swap", partition] as const;

/** How often a running swap is polled. Progress moves a file at a time. */
const POLL_MS = 3000;
/** How often it is polled otherwise. A swap is a partition's state, not this
 *  page's: another admin can start one, and the answer here can be "none yet"
 *  or a swap that has since been replaced by a newer one. Slowly, because none
 *  of that is this reader's own doing, but never not at all — the page would
 *  keep showing the state it opened on until it is reloaded. */
const IDLE_POLL_MS = 30000;

/** How often to ask again, given what the last answer was. */
export function swapPollInterval(status: EmbedderSwap["status"] | undefined): number {
  return status === "running" ? POLL_MS : IDLE_POLL_MS;
}

/** A partition's embedder swap: the running one, or how the last one ended. */
export function useEmbedderSwap(partition: string | undefined) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: embedderSwapQueryKey(partition ?? ""),
    queryFn: () => getEmbedderSwap(partition as string),
    enabled: !!partition,
    refetchInterval: (q) => swapPollInterval(q.state.data?.status),
  });
  const swap = query.data ?? null;
  const status = swap?.status;

  // A swap ending changes what the partition page shows — its embedder, the
  // per-file embedder breakdown — so refetch those once, on the transition.
  // The partition is tracked too: moving from a running partition to one whose
  // last swap ended is not that swap ending.
  const previous = useRef({ partition, status });
  useEffect(() => {
    const wasRunning = previous.current.partition === partition && previous.current.status === "running";
    previous.current = { partition, status };
    if (!wasRunning || status === "running" || !partition) return;
    queryClient.invalidateQueries({ queryKey: ["partition", partition] });
    queryClient.invalidateQueries({ queryKey: ["partitions"] });
    if (status === "completed") toast.success(`${partition} now uses ${swap?.target_embedder}`);
    if (status === "failed") toast.error(`Re-embedding ${partition} failed`);
  }, [status, partition, queryClient, swap?.target_embedder]);

  return { swap, running: status === "running" };
}
