import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EmbedderSwap } from "@/lib/api/partitions";

const swaps = vi.hoisted(() => new Map<string, EmbedderSwap>());
vi.mock("@/lib/api/partitions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/partitions")>()),
  getEmbedderSwap: vi.fn(async (name: string) => swaps.get(name) ?? null),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { embedderSwapQueryKey, swapPollInterval, useEmbedderSwap } from "./use-embedder-swap";

const swap = (partition: string, status: EmbedderSwap["status"]): EmbedderSwap => ({
  partition,
  source_embedder: "default",
  target_embedder: "bge-m3",
  status,
  files_total: 8,
  files_done: status === "running" ? 2 : 8,
  error: null,
  started_at: "2026-09-14T00:00:00Z",
  updated_at: "2026-09-14T00:00:00Z",
  finished_at: status === "running" ? null : "2026-09-14T00:01:00Z",
});

function setup(initial: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  for (const [name, value] of swaps) client.setQueryData(embedderSwapQueryKey(name), value);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(({ partition }) => useEmbedderSwap(partition), {
    wrapper,
    initialProps: { partition: initial },
  });
  return { client, ...hook };
}

beforeEach(() => {
  swaps.clear();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("useEmbedderSwap", () => {
  it("announces a running swap that completes", async () => {
    swaps.set("a", swap("a", "running"));
    const { client, result } = setup("a");
    expect(result.current.running).toBe(true);

    act(() => client.setQueryData(embedderSwapQueryKey("a"), swap("a", "completed")));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("a now uses bge-m3"));
  });

  it("keeps asking once a swap ends, and when there is none", () => {
    // Another admin can start one, and a finished swap can be replaced by a
    // newer one: a page that stopped asking would show neither until reloaded.
    expect(swapPollInterval("running")).toBeLessThan(swapPollInterval("completed"));
    expect(swapPollInterval("completed")).toBe(swapPollInterval(undefined));
    expect(swapPollInterval(undefined)).toBeGreaterThan(0);
  });

  it("announces nothing when moving from a running partition to one whose swap already ended", () => {
    swaps.set("a", swap("a", "running"));
    swaps.set("b", swap("b", "completed"));
    const { rerender, result } = setup("a");

    rerender({ partition: "b" });

    expect(result.current.swap?.status).toBe("completed");
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
