import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { getEmbedderSwap } from "@/lib/api/partitions";

export const embedderSwapQueryKey = (partition: string) => ["embedder-swap", partition] as const;

/** How often a running swap is polled. Progress moves a file at a time. */
const POLL_MS = 3000;

/** A partition's embedder swap (#762 F4): the running one, or how the last one
 *  ended. Polled only while it runs, since a finished swap never changes. */
export function useEmbedderSwap(partition: string | undefined) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: embedderSwapQueryKey(partition ?? ""),
    queryFn: () => getEmbedderSwap(partition as string),
    enabled: !!partition,
    refetchInterval: (q) => (q.state.data?.status === "running" ? POLL_MS : false),
  });
  const swap = query.data ?? null;
  const status = swap?.status;

  // A swap ending changes what the partition page shows — its embedder, the
  // per-file embedder breakdown — so refetch those once, on the transition.
  const previous = useRef(status);
  useEffect(() => {
    const wasRunning = previous.current === "running";
    previous.current = status;
    if (!wasRunning || status === "running" || !partition) return;
    queryClient.invalidateQueries({ queryKey: ["partition", partition] });
    queryClient.invalidateQueries({ queryKey: ["partitions"] });
    if (status === "completed") toast.success(`${partition} now uses ${swap?.target_embedder}`);
    if (status === "failed") toast.error(`Re-embedding ${partition} failed`);
  }, [status, partition, queryClient, swap?.target_embedder]);

  return { swap, running: status === "running" };
}
