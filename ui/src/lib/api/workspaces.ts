import { request } from "./client";

const P = "/partition";
const enc = encodeURIComponent;

export interface Workspace {
  workspace_id: string;
  partition_name: string;
  display_name: string | null;
  created_by: number;
  // Legacy workspace rows can predate the nullable created_at column value.
  created_at: string | null;
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
  expectedCreatedAt?: string | null,
): Promise<{ file_ids: string[] }> {
  const params = new URLSearchParams();
  if (expectedCreatedAt === null) params.set("expected_created_at_is_null", "true");
  else if (expectedCreatedAt !== undefined) params.set("expected_created_at", expectedCreatedAt);
  const serializedParams = params.toString();
  const query = serializedParams ? `?${serializedParams}` : "";
  return request<{ file_ids: string[] }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files${query}`,
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
  expectedCreatedAt?: string | null,
): Promise<{ status: "added"; file_ids: string[] }> {
  const body: {
    file_ids: string[];
    expected_created_at?: string;
    expected_created_at_is_null?: boolean;
  } = { file_ids: fileIds };
  if (expectedCreatedAt === null) body.expected_created_at_is_null = true;
  else if (expectedCreatedAt !== undefined) body.expected_created_at = expectedCreatedAt;
  return request<{ status: "added"; file_ids: string[] }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function removeFileFromWorkspace(
  partition: string,
  workspaceId: string,
  fileId: string,
  expectedCreatedAt?: string | null,
): Promise<{ status: "removed" }> {
  const params = new URLSearchParams();
  if (expectedCreatedAt === null) params.set("expected_created_at_is_null", "true");
  else if (expectedCreatedAt !== undefined) params.set("expected_created_at", expectedCreatedAt);
  const serializedParams = params.toString();
  const query = serializedParams ? `?${serializedParams}` : "";
  return request<{ status: "removed" }>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}/files/${enc(fileId)}${query}`,
    { method: "DELETE" },
  );
}

export function deleteWorkspace(
  partition: string,
  workspaceId: string,
  keepFiles: boolean,
  expectedCreatedAt: string | null,
): Promise<DeleteWorkspaceResponse> {
  const params = new URLSearchParams({
    keep_files: String(keepFiles),
  });
  if (expectedCreatedAt === null) params.set("expected_created_at_is_null", "true");
  else params.set("expected_created_at", expectedCreatedAt);
  return request<DeleteWorkspaceResponse>(
    `${P}/${enc(partition)}/workspaces/${enc(workspaceId)}?${params.toString()}`,
    { method: "DELETE" },
  );
}
