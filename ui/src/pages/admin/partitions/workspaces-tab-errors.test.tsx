import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { listPartitionFiles, listPartitions } from "@/lib/api/partitions";
import { addFilesToWorkspace, listWorkspaceFiles, listWorkspaces, removeFileFromWorkspace } from "@/lib/api/workspaces";
import { WorkspacesTab } from "./workspaces-tab";

vi.mock("@/lib/api/partitions");
vi.mock("@/lib/api/workspaces");
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7, is_admin: false } }) }));
vi.mock("@/lib/permissions", () => ({
  usePermissions: () => ({ canWrite: () => true, canConfigurePartition: () => true }),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const partition = "legal";
const workspace = {
  workspace_id: "contracts",
  partition_name: partition,
  display_name: "Contracts",
  created_by: 7,
  created_at: "2026-01-01T12:00:00",
};
const deletedFile = { file_id: "old", partition, link: "", filename: "Old contract.pdf" };
const remainingFile = { file_id: "current", partition, link: "", filename: "Current contract.pdf" };

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkspacesTab partition={partition} />
    </QueryClientProvider>,
  );
}

describe("Workspace membership errors", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listPartitions).mockResolvedValue({
      partitions: [{
        partition,
        name: partition,
        role: "owner",
        exists: true,
        description: "",
        embedder: "",
        dimension: null,
        collection_name: null,
        chat_history_depth: 0,
        chat_llm: null,
        document_count: 2,
        indexation_preset: "",
        retrieval_preset: "",
        indexation_pipeline: {},
        retrieval_pipeline: {},
        created_at: null,
      }],
    });
    vi.mocked(listWorkspaces).mockResolvedValue({ workspaces: [workspace] });
    vi.mocked(listWorkspaceFiles).mockResolvedValue({ file_ids: [] });
    vi.mocked(listPartitionFiles).mockResolvedValue({ files: [deletedFile, remainingFile] });
  });

  it("refreshes the file catalogue and keeps the remaining selections after a missing-file 404", async () => {
    const detail = "File IDs not found in partition 'legal': ['old']";
    vi.mocked(addFilesToWorkspace)
      .mockRejectedValueOnce(new ApiError(404, { detail }))
      .mockResolvedValueOnce({ status: "added", file_ids: [remainingFile.file_id] });
    vi.mocked(listPartitionFiles)
      .mockResolvedValueOnce({ files: [deletedFile, remainingFile] })
      .mockResolvedValue({ files: [remainingFile] });
    const user = userEvent.setup();
    renderTab();

    await user.click(await screen.findByRole("button", { name: "Add files" }));
    const picker = within(await screen.findByRole("dialog"));
    await user.type(picker.getByPlaceholderText("Search files"), "contract");
    await user.click(await picker.findByRole("checkbox", { name: /Old contract/ }));
    await user.click(picker.getByRole("checkbox", { name: /Current contract/ }));
    await user.click(picker.getByRole("button", { name: "Add 2 files" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(detail));
    expect(listPartitionFiles).toHaveBeenCalledTimes(2);
    expect(picker.queryByRole("checkbox", { name: /Old contract/ })).toBeNull();
    expect(picker.getByRole("checkbox", { name: /Current contract/ }).getAttribute("data-state")).toBe("checked");
    expect(picker.getByPlaceholderText("Search files")).toHaveProperty("value", "contract");

    await user.click(picker.getByRole("button", { name: "Add 1 file" }));
    expect(addFilesToWorkspace).toHaveBeenLastCalledWith(partition, "contracts", ["current"], workspace.created_at);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it.each([
    [404, "Workspace not found"],
    [409, "Workspace changed after it was loaded. Refresh and retry."],
  ])("closes the picker when the workspace changed (%i)", async (status, detail) => {
    vi.mocked(addFilesToWorkspace).mockRejectedValueOnce(new ApiError(status, { detail }));
    const user = userEvent.setup();
    renderTab();

    await user.click(await screen.findByRole("button", { name: "Add files" }));
    const picker = within(await screen.findByRole("dialog"));
    await user.click(await picker.findByRole("checkbox", { name: /Current contract/ }));
    await user.click(picker.getByRole("button", { name: "Add 1 file" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(toast.error).toHaveBeenCalledWith(
      "Workspace changed or was deleted. Review the refreshed list before adding again.",
    );
  });

  it("shows the server message and refreshes membership when a file was already removed", async () => {
    const detail = "File not found in workspace";
    vi.mocked(listWorkspaceFiles).mockResolvedValueOnce({ file_ids: [deletedFile.file_id] });
    vi.mocked(removeFileFromWorkspace).mockRejectedValueOnce(new ApiError(404, { detail }));
    const user = userEvent.setup();
    renderTab();

    await user.click(await screen.findByRole("button", { name: "View files" }));
    const removeButton = await screen.findByRole("button", { name: "Remove old from contracts" });
    await user.click(removeButton);

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(detail));
    expect(await screen.findByText("No files in this workspace yet.")).toBeTruthy();
    expect(listPartitionFiles).toHaveBeenCalledTimes(2);
  });
});
