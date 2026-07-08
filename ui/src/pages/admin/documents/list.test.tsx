import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deleteFile } from "@/lib/api/indexing";
import DocumentListPage from "./list";

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({
    canWrite: () => true,
  }),
}));

vi.mock("@/lib/api/partitions", () => ({
  listPartitions: vi.fn().mockResolvedValue({
    partitions: [
      {
        partition: "docs",
        name: "docs",
        role: "owner",
        created_at: null,
        document_count: 2,
      },
    ],
  }),
}));

vi.mock("@/lib/api/documents", () => ({
  listPartitionFiles: vi.fn().mockResolvedValue({
    files: [
      {
        file_id: "file-a",
        partition: "docs",
        filename: "a.pdf",
        mimetype: "application/pdf",
        indexed_at: "2026-01-01T00:00:00Z",
      },
      {
        file_id: "file-b",
        partition: "docs",
        filename: "b.pdf",
        mimetype: "application/pdf",
        indexed_at: "2026-01-02T00:00:00Z",
      },
    ],
  }),
}));

vi.mock("@/lib/api/indexing", () => ({
  uploadFile: vi.fn(),
  deleteFile: vi.fn().mockResolvedValue(undefined),
  newFileId: vi.fn(() => "new-file-id"),
}));

const deleteFileMock = vi.mocked(deleteFile);

function renderDocuments() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DocumentListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DocumentListPage bulk actions", () => {
  beforeEach(() => {
    deleteFileMock.mockClear();
  });

  it("selects all documents from the table header and deletes the selected files", async () => {
    renderDocuments();

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.getByText("b.pdf")).not.toBeNull();
    expect(screen.queryByText(/selected/i)).toBeNull();

    await userEvent.click(screen.getByRole("checkbox", { name: /select visible rows/i }));

    expect(screen.getByText("2 selected")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /delete selected files/i }));
    await userEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(deleteFileMock).toHaveBeenCalledWith("docs", "file-a"));
    expect(deleteFileMock).toHaveBeenCalledWith("docs", "file-b");
  });
});
