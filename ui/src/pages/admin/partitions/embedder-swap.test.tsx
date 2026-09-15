import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelEndpointResponse } from "@/lib/api/models";
import type { EmbedderSwap } from "@/lib/api/partitions";

const api = vi.hoisted(() => ({
  cancelEmbedderSwap: vi.fn(),
  startEmbedderSwap: vi.fn(),
}));
vi.mock("@/lib/api/partitions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/partitions")>()),
  ...api,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ChangeEmbedderDialog, EmbedderSwapBanner, EmbedderSwapNotice } from "./embedder-swap";

const endpoint = (name: string, model_name: string, is_default = false): ModelEndpointResponse => ({
  name,
  model_type: "embedder",
  endpoint: `http://${name}:8000/v1`,
  model_name,
  batch_size: 32,
  timeout: 60,
  extra: {},
  is_default,
  vector_field: `vector_${name}`,
  created_at: "",
  updated_at: "",
});
const endpoints = [endpoint("e5", "intfloat/e5", true), endpoint("bge-m3", "BAAI/bge-m3")];

const running: EmbedderSwap = {
  partition: "docs",
  source_embedder: "default",
  target_embedder: "bge-m3",
  status: "running",
  files_total: 8,
  files_done: 2,
  error: null,
  started_at: "2026-09-14T00:00:00Z",
  updated_at: "2026-09-14T00:00:00Z",
  finished_at: null,
};

function withQueryClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.cancelEmbedderSwap.mockReset();
  api.startEmbedderSwap.mockReset();
});

describe("EmbedderSwapBanner", () => {
  it("shows progress and what stays in use until the swap completes", () => {
    withQueryClient(<EmbedderSwapBanner swap={running} canCancel endpoints={endpoints} />);

    expect(screen.getByText("Re-embedding with bge-m3")).not.toBeNull();
    expect(screen.getByText("2 of 8 files")).not.toBeNull();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("2");
    // The `default` alias is named by what it resolves to.
    expect(screen.getByText(/Searches keep using e5 until every file is done/)).not.toBeNull();
  });

  it("cancels the running swap", async () => {
    api.cancelEmbedderSwap.mockResolvedValue({ ...running, status: "cancelled" });
    const user = userEvent.setup();
    withQueryClient(<EmbedderSwapBanner swap={running} canCancel endpoints={endpoints} />);

    await user.click(screen.getByRole("button", { name: "Cancel re-embedding" }));

    expect(api.cancelEmbedderSwap).toHaveBeenCalledWith("docs");
  });

  it("offers no cancel to someone who cannot change the partition", () => {
    withQueryClient(<EmbedderSwapBanner swap={running} canCancel={false} endpoints={endpoints} />);

    expect(screen.queryByRole("button", { name: "Cancel re-embedding" })).toBeNull();
  });

  it("explains a failure and that the partition did not move", () => {
    const failed = { ...running, status: "failed" as const, error: "vLLM unreachable" };
    withQueryClient(<EmbedderSwapBanner swap={failed} canCancel endpoints={endpoints} />);

    expect(screen.getByText("vLLM unreachable")).not.toBeNull();
    expect(screen.getByText(/still uses e5\. Starting again skips the 2 files already done/)).not.toBeNull();
  });

  it.each(["completed", "cancelled"] as const)("shows nothing once %s", (status) => {
    const { container } = withQueryClient(
      <EmbedderSwapBanner swap={{ ...running, status }} canCancel endpoints={endpoints} />,
    );

    expect(container.textContent).toBe("");
  });
});

describe("EmbedderSwapNotice", () => {
  it("shows the same progress on a page away from the partition, and links back", () => {
    withQueryClient(<EmbedderSwapNotice swap={running} />);

    expect(screen.getByText("Re-embedding docs with bge-m3")).not.toBeNull();
    expect(screen.getByText("2 of 8 files")).not.toBeNull();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("2");
    expect(screen.getByRole("link", { name: "Partition settings" }).getAttribute("href")).toBe(
      "/partitions/docs",
    );
  });

  it("offers no cancel — that stays owner-gated on the partition page", () => {
    withQueryClient(<EmbedderSwapNotice swap={running} />);

    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it.each(["completed", "cancelled", "failed"] as const)(
    "stays out of the way once %s",
    (status) => {
      const { container } = withQueryClient(<EmbedderSwapNotice swap={{ ...running, status }} />);

      expect(container.textContent).toBe("");
    },
  );

  it("shows nothing when no swap ever ran", () => {
    const { container } = withQueryClient(<EmbedderSwapNotice swap={null} />);

    expect(container.textContent).toBe("");
  });
});

describe("ChangeEmbedderDialog", () => {
  it("starts re-embedding with the chosen embedder", async () => {
    api.startEmbedderSwap.mockResolvedValue(running);
    const onClose = vi.fn();
    const user = userEvent.setup();
    withQueryClient(
      <ChangeEmbedderDialog
        partition="docs"
        currentEmbedder="default"
        documentCount={8}
        endpoints={endpoints}
        initialTarget="bge-m3"
        onClose={onClose}
      />,
    );

    expect(screen.getByText(/8 files are re-embedded from their stored text/)).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Start re-embedding" }));

    expect(api.startEmbedderSwap).toHaveBeenCalledWith("docs", "bge-m3");
    expect(onClose).toHaveBeenCalled();
  });

  it("says a swap onto the current embedder only redoes drifted files", () => {
    withQueryClient(
      <ChangeEmbedderDialog
        partition="docs"
        currentEmbedder="default"
        documentCount={8}
        endpoints={endpoints}
        initialTarget="e5"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/only files indexed with another model are re-embedded/)).not.toBeNull();
  });

  it("cannot start without a target", () => {
    withQueryClient(
      <ChangeEmbedderDialog
        partition="docs"
        currentEmbedder="e5"
        documentCount={8}
        endpoints={endpoints}
        initialTarget=""
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Start re-embedding" }).hasAttribute("disabled")).toBe(true);
  });
});
