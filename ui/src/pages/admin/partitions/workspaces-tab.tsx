import { useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { QueryErrorNotice } from "@/components/shared/query-error-notice";
import { listPartitionFiles, listPartitions, type PartitionFile } from "@/lib/api/partitions";
import {
  addFilesToWorkspace,
  createWorkspace,
  deleteWorkspace,
  listWorkspaceFiles,
  listWorkspaces,
  removeFileFromWorkspace,
  type CreateWorkspaceRequest,
  type Workspace,
} from "@/lib/api/workspaces";
import { usePermissions } from "@/lib/permissions";

const workspaceIdPattern = /^[A-Za-z0-9_-]+$/;
const fileLabel = (file: PartitionFile) => file.filename || file.file_id;
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "The request could not be completed.";

export function WorkspacesTab({ partition }: { partition: string }) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [createOpen, setCreateOpen] = useState(false);
  const [workspaceId, setWorkspaceId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<Set<string>>(() => new Set());
  const [addTarget, setAddTarget] = useState<Workspace | null>(null);
  const [selectedFileIds, setSelectedFileIds] = useState<string[]>([]);
  const [fileSearch, setFileSearch] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Workspace | null>(null);
  const [keepFiles, setKeepFiles] = useState(true);

  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const role = partitionsQuery.data?.partitions.find((item) => item.partition === partition)?.role;
  const canEdit = permissions.canWrite(role);
  const canDelete = permissions.canConfigurePartition(role);

  const workspacesQuery = useQuery({
    queryKey: ["partition-workspaces", partition],
    queryFn: () => listWorkspaces(partition),
  });
  const workspaces = workspacesQuery.data?.workspaces ?? [];
  const workspaceFileQueries = useQueries({
    queries: workspaces.map((workspace) => ({
      queryKey: ["partition-workspace-files", partition, workspace.workspace_id],
      queryFn: () => listWorkspaceFiles(partition, workspace.workspace_id),
      enabled:
        expandedWorkspaceIds.has(workspace.workspace_id) ||
        addTarget?.workspace_id === workspace.workspace_id,
    })),
  });
  const addTargetIndex = addTarget
    ? workspaces.findIndex((workspace) => workspace.workspace_id === addTarget.workspace_id)
    : -1;
  const addTargetFilesQuery =
    addTargetIndex >= 0 ? workspaceFileQueries[addTargetIndex] : undefined;
  const workspaceFiles = useMemo(
    () =>
      new Map<string, string[]>(
        workspaces.map(
          (workspace, index) =>
            [workspace.workspace_id, workspaceFileQueries[index]?.data?.file_ids ?? []] as const,
        ),
      ),
    [workspaces, workspaceFileQueries],
  );
  const partitionFilesQuery = useQuery({
    queryKey: ["partition-files", partition],
    queryFn: () => listPartitionFiles(partition),
    enabled: addTarget !== null || expandedWorkspaceIds.size > 0,
  });
  const partitionFiles = partitionFilesQuery.data?.files ?? [];

  const refreshWorkspaceData = async (workspaceIdToRefresh?: string) => {
    await queryClient.invalidateQueries({ queryKey: ["partition-workspaces", partition] });
    if (workspaceIdToRefresh) {
      await queryClient.invalidateQueries({
        queryKey: ["partition-workspace-files", partition, workspaceIdToRefresh],
      });
    }
  };

  const createMutation = useMutation({
    mutationFn: ({
      partition: targetPartition,
      body,
    }: {
      partition: string;
      body: CreateWorkspaceRequest;
    }) => createWorkspace(targetPartition, body),
    onSuccess: async (_result, variables) => {
      await refreshWorkspaceData();
      toast.success(`Workspace “${variables.body.display_name || variables.body.workspace_id}” created.`);
      setCreateOpen(false);
      setWorkspaceId("");
      setDisplayName("");
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const addMutation = useMutation({
    mutationFn: ({ target, fileIds }: { target: Workspace; fileIds: string[] }) =>
      addFilesToWorkspace(partition, target.workspace_id, fileIds),
    onSuccess: async (_result, variables) => {
      await refreshWorkspaceData(variables.target.workspace_id);
      toast.success("Files added to workspace.");
      setAddTarget(null);
      setSelectedFileIds([]);
      setFileSearch("");
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const removeMutation = useMutation({
    mutationFn: ({ target, fileId }: { target: Workspace; fileId: string }) =>
      removeFileFromWorkspace(partition, target.workspace_id, fileId),
    onSuccess: async (_result, variables) => {
      await refreshWorkspaceData(variables.target.workspace_id);
      toast.success("File removed from workspace.");
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const deleteMutation = useMutation({
    mutationFn: ({ target, keepFiles: retain }: { target: Workspace; keepFiles: boolean }) =>
      deleteWorkspace(partition, target.workspace_id, retain),
    onSuccess: async (result, variables) => {
      await refreshWorkspaceData();
      await queryClient.invalidateQueries({ queryKey: ["partition-files", partition] });
      if (!variables.keepFiles && result.orphaned_files_failed.length > 0) {
        toast.error(
          `Workspace deleted, but cleanup failed for ${result.orphaned_files_failed.length} orphaned file(s).`,
        );
      } else if (variables.keepFiles) {
        toast.success(
          `Workspace deleted. ${result.kept_files} file${result.kept_files === 1 ? "" : "s"} kept in the partition.`,
        );
      } else {
        toast.success(
          `Workspace deleted. ${result.orphaned_files_deleted} orphaned file${result.orphaned_files_deleted === 1 ? "" : "s"} deleted.`,
        );
      }
      setDeleteTarget(null);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  const normalizedWorkspaceId = workspaceId.trim();
  const validWorkspaceId = workspaceIdPattern.test(normalizedWorkspaceId);
  const existingWorkspaceIds = new Set(workspaces.map((workspace) => workspace.workspace_id));
  const addableFiles = useMemo(() => {
    if (!addTarget) return [];
    const currentFileIds = new Set(workspaceFiles.get(addTarget.workspace_id) ?? []);
    const query = fileSearch.trim().toLowerCase();
    return partitionFiles
      .filter((file) => !currentFileIds.has(file.file_id))
      .filter((file) => !query || `${fileLabel(file)} ${file.file_id}`.toLowerCase().includes(query))
      .sort((left, right) => fileLabel(left).localeCompare(fileLabel(right)));
  }, [addTarget, fileSearch, partitionFiles, workspaceFiles]);

  const toggleFile = (fileId: string, checked: boolean) => {
    setSelectedFileIds((current) =>
      checked ? [...new Set([...current, fileId])] : current.filter((id) => id !== fileId),
    );
  };

  const toggleWorkspaceFiles = (targetWorkspaceId: string) => {
    setExpandedWorkspaceIds((current) => {
      const next = new Set(current);
      if (next.has(targetWorkspaceId)) next.delete(targetWorkspaceId);
      else next.add(targetWorkspaceId);
      return next;
    });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Workspaces in {partition}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Group existing partition files for workspace-scoped search and chat. Files are shared by
            reference; adding or removing a workspace membership does not copy a file.
          </p>
        </div>
        {canEdit && (
          <Button onClick={() => setCreateOpen(true)}>
            <Plus /> Create workspace
          </Button>
        )}
      </div>

      {role === "viewer" && (
        <p className="text-sm text-muted-foreground">
          You can view workspaces. An editor can create workspaces and change file membership; an
          owner can also delete them.
        </p>
      )}

      {workspacesQuery.isLoading ? (
        <div className="space-y-3">
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
        </div>
      ) : workspacesQuery.isError ? (
        <QueryErrorNotice
          title="Workspaces could not be loaded"
          message={errorMessage(workspacesQuery.error)}
          isRetrying={workspacesQuery.isFetching}
          onRetry={() => void workspacesQuery.refetch()}
        />
      ) : workspaces.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No workspaces in this partition yet. Create one to group files for scoped search and chat.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {workspaces.map((workspace, index) => {
            const fileQuery = workspaceFileQueries[index];
            const fileIds = fileQuery?.data?.file_ids ?? [];
            const filesExpanded = expandedWorkspaceIds.has(workspace.workspace_id);
            return (
              <Card key={workspace.workspace_id}>
                <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
                  <div className="min-w-0">
                    <CardTitle className="break-words">
                      {workspace.display_name || workspace.workspace_id}
                    </CardTitle>
                    <CardDescription className="mt-1 break-all font-mono">
                      {workspace.workspace_id}
                    </CardDescription>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {canEdit && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setAddTarget(workspace);
                          setSelectedFileIds([]);
                          setFileSearch("");
                        }}
                      >
                        <Plus /> Add files
                      </Button>
                    )}
                    {canDelete && (
                      <Button
                        variant="destructive"
                        size="icon-sm"
                        aria-label={`Delete ${workspace.display_name || workspace.workspace_id}`}
                        title="Delete workspace"
                        onClick={() => {
                          setKeepFiles(true);
                          setDeleteTarget(workspace);
                        }}
                      >
                        <Trash2 />
                      </Button>
                    )}
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <span className="text-sm text-muted-foreground">
                      {fileQuery?.data ? `${fileIds.length} file${fileIds.length === 1 ? "" : "s"}` : "Files"}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-expanded={filesExpanded}
                      onClick={() => toggleWorkspaceFiles(workspace.workspace_id)}
                    >
                      {filesExpanded ? "Hide files" : "View files"}
                    </Button>
                  </div>
                  {filesExpanded && (
                    !fileQuery || (!fileQuery.data && !fileQuery.isError) ? (
                      <p className="text-sm text-muted-foreground">Loading files…</p>
                    ) : fileQuery && fileQuery.isError ? (
                      <QueryErrorNotice
                        title="Workspace files could not be loaded"
                        message={errorMessage(fileQuery.error)}
                        isRetrying={fileQuery.isFetching}
                        onRetry={() => void fileQuery.refetch()}
                      />
                    ) : fileIds.length === 0 ? (
                      <p className="text-sm text-muted-foreground">No files in this workspace yet.</p>
                    ) : (
                      <ul className="divide-y rounded-md border">
                        {fileIds.map((fileId) => {
                          const file = partitionFiles.find((item) => item.file_id === fileId);
                          return (
                            <li
                              key={fileId}
                              className="flex min-w-0 items-center justify-between gap-3 px-3 py-2"
                            >
                              <span className="min-w-0 break-all text-sm" title={fileId}>
                                {file ? fileLabel(file) : fileId}
                                {file && fileLabel(file) !== fileId && (
                                  <span className="ml-2 font-mono text-xs text-muted-foreground">{fileId}</span>
                                )}
                              </span>
                              {canEdit && (
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`Remove ${fileId} from ${workspace.workspace_id}`}
                                  title="Remove from workspace"
                                  disabled={removeMutation.isPending}
                                  onClick={() => removeMutation.mutate({ target: workspace, fileId })}
                                >
                                  <X />
                                </Button>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    )
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create workspace</DialogTitle>
            <DialogDescription>
              Workspace IDs are unique within this partition and may contain letters, numbers,
              hyphens, and underscores.
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!validWorkspaceId || existingWorkspaceIds.has(normalizedWorkspaceId)) return;
              createMutation.mutate({
                partition,
                body: {
                  workspace_id: normalizedWorkspaceId,
                  ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
                },
              });
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="workspace-id">Workspace ID</Label>
              <Input
                id="workspace-id"
                value={workspaceId}
                onChange={(event) => setWorkspaceId(event.target.value)}
                autoComplete="off"
                required
                aria-invalid={workspaceId.length > 0 && !validWorkspaceId}
              />
              {workspaceId.length > 0 && !validWorkspaceId && (
                <p className="text-sm text-destructive">
                  Use only letters, numbers, hyphens, and underscores.
                </p>
              )}
              {existingWorkspaceIds.has(normalizedWorkspaceId) && (
                <p className="text-sm text-destructive">This workspace ID already exists here.</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="workspace-display-name">Display name (optional)</Label>
              <Input
                id="workspace-display-name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                autoComplete="off"
              />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateOpen(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={!validWorkspaceId || existingWorkspaceIds.has(normalizedWorkspaceId) || createMutation.isPending}
              >
                Create workspace
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={addTarget !== null}
        onOpenChange={(open) => {
          if (!open) setAddTarget(null);
        }}
      >
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Add files to {addTarget?.display_name || addTarget?.workspace_id}</DialogTitle>
            <DialogDescription>
              Select existing files from this partition. Removing a file later only removes its
              workspace membership.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={fileSearch}
                onChange={(event) => setFileSearch(event.target.value)}
                placeholder="Search files"
                className="pl-9"
              />
            </div>
            {addTarget && !addTargetFilesQuery?.data && !addTargetFilesQuery?.isError ? (
              <p className="py-6 text-center text-sm text-muted-foreground">Loading workspace membership…</p>
            ) : addTargetFilesQuery && addTargetFilesQuery.isError ? (
              <QueryErrorNotice
                title="Workspace files could not be loaded"
                message={errorMessage(addTargetFilesQuery.error)}
                isRetrying={addTargetFilesQuery.isFetching}
                onRetry={() => void addTargetFilesQuery.refetch()}
              />
            ) : partitionFilesQuery.isLoading ? (
              <p className="py-6 text-center text-sm text-muted-foreground">Loading partition files…</p>
            ) : partitionFilesQuery.isError ? (
              <QueryErrorNotice
                title="Partition files could not be loaded"
                message={errorMessage(partitionFilesQuery.error)}
                isRetrying={partitionFilesQuery.isFetching}
                onRetry={() => void partitionFilesQuery.refetch()}
              />
            ) : addableFiles.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                {fileSearch.trim() ? "No matching files are available." : "All partition files are already in this workspace."}
              </p>
            ) : (
              <ul className="max-h-72 space-y-1 overflow-y-auto rounded-md border p-2">
                {addableFiles.map((file) => (
                  <li key={file.file_id}>
                    <label className="flex cursor-pointer items-start gap-3 rounded-md p-2 hover:bg-muted">
                      <Checkbox
                        checked={selectedFileIds.includes(file.file_id)}
                        onCheckedChange={(checked) => toggleFile(file.file_id, checked === true)}
                      />
                      <span className="min-w-0 text-sm">
                        <span className="block break-words">{fileLabel(file)}</span>
                        <span className="block break-all font-mono text-xs text-muted-foreground">
                          {file.file_id}
                        </span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setAddTarget(null)}>
              Cancel
            </Button>
            <Button
              disabled={
                !addTarget ||
                !addTargetFilesQuery?.data ||
                addTargetFilesQuery?.isError ||
                selectedFileIds.length === 0 ||
                addMutation.isPending
              }
              onClick={() => {
                if (addTarget && selectedFileIds.length) {
                  addMutation.mutate({ target: addTarget, fileIds: selectedFileIds });
                }
              }}
            >
              Add {selectedFileIds.length || ""} file{selectedFileIds.length === 1 ? "" : "s"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete {deleteTarget?.display_name || deleteTarget?.workspace_id}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This removes the workspace and its file memberships. Files shared with other
              workspaces remain available.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2 rounded-md border p-3">
            <label className="flex cursor-pointer items-start gap-3">
              <Checkbox checked={keepFiles} onCheckedChange={(checked) => setKeepFiles(checked === true)} />
              <span className="text-sm">
                <span className="font-medium">Keep indexed files in the partition</span>
                <span className="mt-1 block text-muted-foreground">
                  When unchecked, workspace-owned files that are not in another workspace may be
                  deleted from the partition.
                </span>
              </span>
            </label>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={(event) => {
                event.preventDefault();
                if (deleteTarget) deleteMutation.mutate({ target: deleteTarget, keepFiles });
              }}
            >
              Delete workspace
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
