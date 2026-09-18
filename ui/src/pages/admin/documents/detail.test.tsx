import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getFileDetail, listFileChunks } from "@/lib/api/documents";
import DocumentDetailPage from "./detail";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({ canWrite: () => false }),
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

function renderDocumentDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/documents/docs/file-1"]}>
        <Routes>
          <Route path="/documents/:partition/:fileId" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
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
});
