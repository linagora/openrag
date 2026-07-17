import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MouseEvent as ReactMouseEvent } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import type { Action } from "sonner";
import { deleteFile, uploadFile } from "@/lib/api/indexing";
import DocumentListPage from "./list";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

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
const uploadFileMock = vi.mocked(uploadFile);
const toastSuccessMock = vi.mocked(toast.success);

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

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
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DocumentListPage", () => {
  beforeEach(() => {
    deleteFileMock.mockClear();
    uploadFileMock.mockReset();
    toastSuccessMock.mockClear();
  });

  it("labels row icon actions", async () => {
    renderDocuments();

    expect(await screen.findByRole("link", { name: /view a\.pdf/i })).not.toBeNull();
    expect(screen.getByRole("button", { name: /delete a\.pdf/i })).not.toBeNull();
  });

  it("keeps filenames constrained while exposing the full name", async () => {
    renderDocuments();

    const fileLink = await screen.findByRole("link", { name: "a.pdf" });
    expect(fileLink.getAttribute("title")).toBe("a.pdf");
    expect(fileLink.className).toContain("truncate");
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

  it("summarizes queued files and links directly to the jobs view", async () => {
    uploadFileMock.mockResolvedValue({ task_status_url: "/queue/task-1" });
    renderDocuments();

    await screen.findByText("a.pdf");
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));

    const files = [
      new File(["first"], "first.txt", { type: "text/plain" }),
      new File(["second"], "second.txt", { type: "text/plain" }),
    ];
    await userEvent.upload(screen.getByLabelText("Files"), files);
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(uploadFileMock).toHaveBeenCalledTimes(2));
    expect(toastSuccessMock).toHaveBeenCalledWith(
      "2 file(s) queued for indexing.",
      expect.objectContaining({
        action: expect.objectContaining({ label: "View Jobs" }),
      }),
    );

    const options = toastSuccessMock.mock.calls[0][1];
    const action = options?.action as unknown as Action;
    act(() => action.onClick({} as ReactMouseEvent<HTMLButtonElement>));

    expect(screen.getByTestId("location").textContent).toBe("/jobs");
  });
});
