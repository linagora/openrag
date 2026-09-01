import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MouseEvent as ReactMouseEvent } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import type { Action } from "sonner";
import { deleteFile, uploadFile } from "@/lib/api/indexing";
import { getQueueInfo, type QueueInfo } from "@/lib/api/jobs";
import { downloadCsv } from "@/lib/csv";
import { useActiveJobsCount } from "@/lib/jobs-queries";
import DocumentListPage from "./list";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const permissions = vi.hoisted(() => ({
  canWrite: vi.fn(() => true),
  superAdminModeResolved: true,
}));

vi.mock("@/lib/permissions", () => ({
  usePermissions: () => permissions,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ user: { id: 7, is_admin: true } }),
}));

vi.mock("@/lib/api/jobs", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/jobs")>("@/lib/api/jobs");
  return { ...actual, getQueueInfo: vi.fn() };
});

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
        indexed_at: new Date(2026, 0, 2, 0, 30).toISOString(),
      },
    ],
  }),
}));

vi.mock("@/lib/api/indexing", () => ({
  uploadFile: vi.fn(),
  deleteFile: vi.fn().mockResolvedValue(undefined),
  newFileId: vi.fn(() => "new-file-id"),
}));

vi.mock("@/lib/csv", () => ({
  downloadCsv: vi.fn(),
}));

const deleteFileMock = vi.mocked(deleteFile);
const uploadFileMock = vi.mocked(uploadFile);
const getQueueInfoMock = vi.mocked(getQueueInfo);
const downloadCsvMock = vi.mocked(downloadCsv);
const toastSuccessMock = vi.mocked(toast.success);

const queueInfo = (active: number): QueueInfo => ({
  workers: { total_slots: 4, pool_size: 2, max_per_actor: 2 },
  tasks: {
    active,
    active_statuses: { QUEUED: active, SERIALIZING: 0 },
    total_completed: 0,
    total_cancelled: 0,
    total_failed: 0,
  },
});

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function ActiveJobsProbe() {
  return <output data-testid="active-jobs">{useActiveJobsCount()}</output>;
}

function renderDocuments(initialEntries = ["/documents"], includeJobsProbe = false) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const ui = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        <DocumentListPage />
        <LocationProbe />
        {includeJobsProbe && <ActiveJobsProbe />}
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(ui());
  return Object.assign(view, {
    rerenderDocuments: () => view.rerender(ui()),
  });
}

describe("DocumentListPage", () => {
  beforeEach(() => {
    sessionStorage.clear();
    permissions.canWrite.mockImplementation(() => true);
    permissions.superAdminModeResolved = true;
    deleteFileMock.mockClear();
    uploadFileMock.mockReset();
    getQueueInfoMock.mockReset();
    downloadCsvMock.mockClear();
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

  it("filters documents by file name and indexed date before exporting", async () => {
    renderDocuments();

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    await userEvent.type(screen.getByLabelText("Search files"), "b");
    await userEvent.type(screen.getByLabelText("Indexed since"), "2026-01-02");

    expect(screen.queryByText("a.pdf")).toBeNull();
    expect(screen.getByText("b.pdf")).not.toBeNull();
    expect(screen.getByText("1 of 2 file(s)")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /export csv/i }));

    expect(downloadCsvMock).toHaveBeenCalledWith(
      "openrag-documents-docs.csv",
      expect.any(Array),
      [expect.objectContaining({ file_id: "file-b" })],
    );
  });

  it("reports CSV download failures", async () => {
    downloadCsvMock.mockImplementationOnce(() => {
      throw new Error("downloads unavailable");
    });
    renderDocuments();

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /export csv/i }));

    expect(toast.error).toHaveBeenCalledWith("CSV export failed: downloads unavailable");
  });

  it("opens the upload dialog for a partition upload link", async () => {
    renderDocuments(["/documents?partition=docs&upload=1"]);

    const dialog = await screen.findByRole("dialog");
    expect(dialog).not.toBeNull();
    expect(screen.getByText(/Index one or more files into/i)).not.toBeNull();
    expect(dialog.textContent).toContain("docs");
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs"));
  });

  it("does not open upload when the requested partition falls back", async () => {
    renderDocuments(["/documents?partition=missing&upload=1"]);

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs"));
  });

  it("keeps the upload route while super-admin write access is still resolving", async () => {
    permissions.canWrite.mockImplementation(() => false);
    permissions.superAdminModeResolved = false;

    renderDocuments(["/documents?partition=docs&upload=1"]);

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs&upload=1");
  });

  it("opens the upload dialog when delayed write access resolves", async () => {
    permissions.canWrite.mockImplementation(() => false);
    permissions.superAdminModeResolved = false;
    const view = renderDocuments(["/documents?partition=docs&upload=1"]);

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs&upload=1");

    permissions.canWrite.mockImplementation(() => true);
    permissions.superAdminModeResolved = true;
    view.rerenderDocuments();

    expect(await screen.findByRole("dialog")).not.toBeNull();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs"));
  });

  it("clears the upload route after write access is rejected", async () => {
    permissions.canWrite.mockImplementation(() => false);
    permissions.superAdminModeResolved = true;

    renderDocuments(["/documents?partition=docs&upload=1"]);

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs"));
  });

  it("clears upload-only routes without opening a fallback partition", async () => {
    renderDocuments(["/documents?upload=1"]);

    expect(await screen.findByText("a.pdf")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/documents?partition=docs"));
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

  it("refreshes the active Jobs count as soon as uploads are accepted", async () => {
    uploadFileMock.mockResolvedValue({ task_status_url: "/queue/task-1" });
    getQueueInfoMock.mockResolvedValueOnce(queueInfo(0)).mockResolvedValue(queueInfo(3));
    renderDocuments(["/documents"], true);

    await waitFor(() => expect(getQueueInfoMock).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("active-jobs").textContent).toBe("0");
    await userEvent.click(await screen.findByRole("button", { name: "Upload" }));
    await userEvent.upload(screen.getByLabelText("Files"), [
      new File(["first"], "first.txt", { type: "text/plain" }),
      new File(["second"], "second.txt", { type: "text/plain" }),
      new File(["third"], "third.txt", { type: "text/plain" }),
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(screen.getByTestId("active-jobs").textContent).toBe("3"));
  });
});
