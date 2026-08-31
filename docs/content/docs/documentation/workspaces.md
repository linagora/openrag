---
title: Workspaces
description: Group files into workspaces for scoped search and chat
---

Workspaces let you organize files within a partition into named subsets. When searching or chatting, you can target a specific workspace so that only its files are considered — without affecting the underlying partition structure.

---

## Concepts

- A **workspace** belongs to exactly one partition
- A file can belong to **multiple workspaces** (or none)
- Workspaces do not duplicate files — they reference existing partition files
- Deleting a workspace deletes **orphaned files** (files not in any other workspace) from the partition automatically
- Deleting a file removes it from all workspaces it belongs to

---

## Data Model

```mermaid
erDiagram
    partitions ||--o{ workspaces : contains
    workspaces ||--o{ workspace_files : has
    files ||--o{ workspace_files : referenced_by

    workspaces {
        int id PK
        varchar workspace_id UK
        varchar partition_name FK
        varchar display_name
        int created_by FK
        datetime created_at
    }

    workspace_files {
        int id PK
        varchar workspace_id FK
        varchar file_id FK
    }
```

**Constraints:**
- `UniqueConstraint(workspace_id)` — workspace IDs are globally unique
- `UniqueConstraint(workspace_id, file_id)` on `workspace_files` — a file appears at most once per workspace
- Cascade delete: dropping a workspace removes its `workspace_files` rows

---

## API Endpoints

All workspace endpoints live under `/partition/{partition}/workspaces`.

### Workspace CRUD

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/partition/{partition}/workspaces` | Editor | Create a workspace |
| GET | `/partition/{partition}/workspaces` | Viewer | List all workspaces in partition |
| GET | `/partition/{partition}/workspaces/{workspace_id}` | Viewer | Get workspace details |
| DELETE | `/partition/{partition}/workspaces/{workspace_id}` | Owner | Delete workspace (and orphaned files, unless `keep_files=true`) |

### Workspace File Management

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/partition/{partition}/workspaces/{workspace_id}/files` | Editor | Add files to workspace |
| GET | `/partition/{partition}/workspaces/{workspace_id}/files` | Viewer | List files in workspace |
| DELETE | `/partition/{partition}/workspaces/{workspace_id}/files/{file_id}` | Editor | Remove file from workspace |

---

### Create a Workspace

```bash
curl -X POST "$BASE_URL/partition/my-partition/workspaces" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workspace_id": "project-alpha", "display_name": "Project Alpha"}'
```

```json
{"status": "created", "workspace_id": "project-alpha"}
```

### List Workspaces

```bash
curl "$BASE_URL/partition/my-partition/workspaces" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "workspaces": [
    {
      "workspace_id": "project-alpha",
      "partition_name": "my-partition",
      "display_name": "Project Alpha",
      "created_by": 1,
      "created_at": "2026-03-06T10:00:00"
    }
  ]
}
```

### Add Files to a Workspace

```bash
curl -X POST "$BASE_URL/partition/my-partition/workspaces/project-alpha/files" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file_ids": ["report.pdf", "notes.md"]}'
```

```json
{"status": "added", "file_ids": ["report.pdf", "notes.md"]}
```

### Delete a Workspace

```bash
curl -X DELETE "$BASE_URL/partition/my-partition/workspaces/project-alpha" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{"status": "deleted", "orphaned_files_deleted": 1, "orphaned_files_failed": [], "kept_files": 0}
```

Files that belonged **only** to the deleted workspace are automatically removed from the partition. Files shared with other workspaces are preserved.

#### Keeping Orphaned Files (`keep_files=true`)

Pass `?keep_files=true` to delete the workspace and its membership rows **without** touching any files, even ones that would otherwise be orphaned:

```bash
curl -X DELETE "$BASE_URL/partition/my-partition/workspaces/project-alpha?keep_files=true" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{"status": "deleted", "orphaned_files_deleted": 0, "orphaned_files_failed": [], "kept_files": 1}
```

- `kept_files` reports how many files would have been orphaned and were left indexed in the partition instead of being deleted.
- The workspace and its `workspace_files` rows are still removed as usual — only the file-deletion step is skipped.
- Default behavior (parameter absent or `keep_files=false`) is unchanged: orphaned files are purged as before.
- Useful when files may be shared outside of the workspace model (e.g. referenced by an external system) and must never be deleted as a side effect of removing a workspace.

---

## Upload with Workspace Assignment

Files can be added to one or more workspaces at upload time using the `workspace_ids` form parameter:

```bash
curl -X POST "$BASE_URL/indexer/partition/my-partition/file/my-file-id" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@document.pdf" \
  -F 'metadata={"mimetype": "application/pdf"}' \
  -F 'workspace_ids=["project-alpha", "project-beta"]'
```

The `workspace_ids` field accepts a JSON array of workspace IDs. Each workspace must exist in the target partition, otherwise the request is rejected with a 404.

---

## Workspace-Scoped Search

Pass the `workspace` query parameter to restrict search results to files in that workspace:

```bash
curl "$BASE_URL/search/partition/my-partition?text=quarterly+results&workspace=project-alpha" \
  -H "Authorization: Bearer $TOKEN"
```

Only chunks from files belonging to the `project-alpha` workspace are returned.

### Multi-Partition Search

The `workspace` parameter also works with multi-partition search:

```bash
curl "$BASE_URL/search?partitions=my-partition&text=quarterly+results&workspace=project-alpha" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Workspace-Scoped Chat

To scope a chat completion to a workspace, include the `workspace` field in the request metadata:

```bash
curl -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openrag-my-partition",
    "messages": [{"role": "user", "content": "Summarize the Q1 results"}],
    "metadata": {"workspace": "project-alpha"}
  }'
```

The RAG pipeline resolves the workspace to its file list and filters the vector search accordingly.

---

## Deletion Behavior

### Deleting a Workspace

```mermaid
flowchart LR
    A[Delete workspace] --> B[Find workspace files]
    B --> C{File in other workspaces?}
    C -->|Yes| D[Keep file]
    C -->|No| G{keep_files=true?}
    G -->|Yes| D
    G -->|No| E[Delete orphaned file from partition]
    D --> F[Done]
    E --> F
```

### Deleting a File

When a file is deleted from a partition (via `DELETE /partition/{partition}/file/{file_id}`), it is automatically removed from all workspaces that reference it.

### Deleting a Partition

When a partition is deleted, all its workspaces and workspace-file associations are cascade-deleted along with the files.
