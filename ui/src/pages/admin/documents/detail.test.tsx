import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { getFileDetail, listFileChunks } from "@/lib/api/documents";
import { listPartitions } from "@/lib/api/partitions";
import DocumentDetailPage from "./detail";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({ canWrite: (role?: string) => role === "editor" }),
}));

vi.mock("@/lib/api/partitions", () => ({
  listPartitions: vi.fn().mockResolvedValue({
    partitions: [{ partition: "docs", name: "docs", role: "viewer" }],
  }),
}));

vi.mock("@/lib/api/documents", () => ({
  getFileDetail: vi.fn(),
  listFileChunks: vi.fn(),
}));

vi.mock("@/lib/api/indexing", () => ({
  replaceFile: vi.fn(),
  deleteFile: vi.fn(),
  copyFile: vi.fn(),
  newFileId: vi.fn(() => "new-file-id"),
}));

const getFileDetailMock = vi.mocked(getFileDetail);
const listFileChunksMock = vi.mocked(listFileChunks);
const listPartitionsMock = vi.mocked(listPartitions);

beforeAll(() => {
  // cmdk (the copy-target picker) measures and scrolls its list; jsdom implements neither.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.scrollIntoView ??= () => {};
});

function renderDocumentDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/documents/docs/file-1"]}>
        <Routes>
          <Route path="/documents/:partition/:fileId" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

describe("DocumentDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getFileDetailMock.mockResolvedValue({
      metadata: {
        filename: "catalog.pdf",
        mimetype: "application/pdf",
        degraded_stages: ["caption"],
      },
      documents: [],
    });
    listFileChunksMock.mockResolvedValue([
      {
        link: "/extract/chunk-1",
        content: "Indexed content",
        metadata: { _id: "chunk-1", filename: "stale-chunk-name.pdf", page: 7 },
      },
    ]);
  });

  it("shows degradation from the authoritative catalog metadata", async () => {
    renderDocumentDetail();

    expect(await screen.findByRole("heading", { name: "catalog.pdf" })).not.toBeNull();
    expect(screen.getByText("Completed with degradation")).not.toBeNull();
    expect(screen.getByText("Caption")).not.toBeNull();
    expect(getFileDetailMock).toHaveBeenCalledWith("docs", "file-1", 0);

    await userEvent.click(screen.getByRole("tab", { name: "Details" }));
    expect(screen.getByText("page")).not.toBeNull();
    expect(screen.getByText("7")).not.toBeNull();
  });

  /** Older backends omit topic_tags; the group still renders with an empty state. */
  it("shows an empty state when the file has no topic tags", async () => {
    renderDocumentDetail();

    await screen.findByRole("group", { name: "Topic tags" });
    expect(screen.getByText("No topic tags")).not.toBeNull();
  });

  /** Stored tags arrive top-level on the file-detail response and render as badges. */
  it("renders stored topic tags as badges", async () => {
    getFileDetailMock.mockResolvedValue({
      metadata: { filename: "catalog.pdf" },
      documents: [],
      topic_tags: ["finance", "risk"],
    });
    renderDocumentDetail();

    await screen.findByRole("group", { name: "Topic tags" });
    expect(screen.getByText("finance")).not.toBeNull();
    expect(screen.getByText("risk")).not.toBeNull();
  });

  it("disables Copy when the chosen target drops out of the refreshed partition list", async () => {
    listPartitionsMock.mockResolvedValue({
      partitions: [
        { partition: "docs", name: "docs", role: "viewer" },
        { partition: "target", name: "target", role: "editor" },
      ],
    } as Awaited<ReturnType<typeof listPartitions>>);
    const queryClient = renderDocumentDetail();

    await userEvent.click(await screen.findByRole("tab", { name: "Details" }));
    await userEvent.click(screen.getByRole("combobox", { name: "Copy to partition" }));
    await userEvent.click(screen.getByRole("option", { name: "target" }));
    const copy = screen.getByRole("button", { name: "Copy" });
    expect(copy.hasAttribute("disabled")).toBe(false);

    listPartitionsMock.mockResolvedValue({
      partitions: [
        { partition: "docs", name: "docs", role: "viewer" },
        { partition: "other", name: "other", role: "editor" },
      ],
    } as Awaited<ReturnType<typeof listPartitions>>);
    await queryClient.refetchQueries({ queryKey: ["partitions"] });

    await waitFor(() => expect(screen.getByRole("button", { name: "Copy" }).hasAttribute("disabled")).toBe(true));
  });
});
