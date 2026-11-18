import { request } from "./client";

const P = "/partition";
const enc = encodeURIComponent;

export interface Workspace {
  workspace_id: string;
  partition_name: string;
  display_name: string | null;
  created_by: number;
  created_at: string;
}

export interface CreateWorkspaceRequest {
  workspace_id: string;
  display_name?: string;
}

export interface DeleteWorkspaceResponse {
  status: "deleted";
  orphaned_files_deleted: number;
  orphaned_files_failed: string[];
  kept_files: number;
}

export function listWorkspaces(partition: string): Promise<{ workspaces: Workspace[] }> {
  return request<{ workspaces: Workspace[] }>(`${P}/${enc(partition)}/workspaces`);
}

export function listWorkspaceFiles(
  partition: string,
  workspaceId: string,
): Promise<{ file_ids: string[] }> {
  return request<{ file_ids: string[] }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files`,
  );
}

export function createWorkspace(
  partition: string,
  body: CreateWorkspaceRequest,
): Promise<{ status: "created"; workspace_id: string }> {
  return request<{ status: "created"; workspace_id: string }>(`${P}/${enc(partition)}/workspaces`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function addFilesToWorkspace(
  partition: string,
  workspaceId: string,
  fileIds: string[],
): Promise<{ status: "added"; file_ids: string[] }> {
  return request<{ status: "added"; file_ids: string[] }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files`,
    { method: "POST", body: JSON.stringify({ file_ids: fileIds }) },
  );
}

export function removeFileFromWorkspace(
  partition: string,
  workspaceId: string,
  fileId: string,
): Promise<{ status: "removed" }> {
  return request<{ status: "removed" }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files/${enc(fileId)}`,
    { method: "DELETE" },
  );
}

export function deleteWorkspace(
  partition: string,
  workspaceId: string,
  keepFiles: boolean,
): Promise<DeleteWorkspaceResponse> {
  return request<DeleteWorkspaceResponse>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}?keep_files=${keepFiles}`,
    { method: "DELETE" },
  );
}
