---
title: Data Model
description: Database schema for PostgreSQL metadata and Milvus vector storage
---

OpenRAG uses a dual-database architecture:
- **PostgreSQL** for metadata (users, partitions, files, access control, indexing job history)
- **Milvus** for content (document chunks, embeddings, vector search)

---

## PostgreSQL Schema

Implemented using **SQLAlchemy ORM** with PostgreSQL as the backend.

```mermaid
erDiagram
    partitions ||--o{ files : contains
    partitions ||--o{ partition_memberships : has
    partitions ||--o{ workspaces : has
    users ||--o{ partition_memberships : belongs_to
    workspaces ||--o{ workspace_files : has
    files ||--o{ workspace_files : referenced_by
    users |o--o{ jobs : submitted

    partitions {
        int id PK
        varchar partition UK
        datetime created_at
    }

    files {
        int id PK
        varchar file_id
        varchar partition_name FK
        json file_metadata
    }

    users {
        int id PK
        varchar external_user_id UK
        varchar display_name
        varchar token UK
        boolean is_admin
        datetime created_at
        int file_quota
        int file_count
    }

    partition_memberships {
        int id PK
        varchar partition_name FK
        int user_id FK
        varchar role
        datetime added_at
    }

    workspaces {
        int id PK
        varchar workspace_id
        varchar partition_name FK
        varchar display_name
        int created_by FK
        datetime created_at
    }

    workspace_files {
        int id PK
        int workspace_id FK
        int file_id FK
    }

    jobs {
        varchar id PK
        varchar partition
        varchar file_id
        int user_id FK
        varchar status
        varchar error
        datetime created_at
        datetime updated_at
        datetime started_at
        datetime completed_at
    }
```

### `users`

Stores information about API users and administrators.

| Column         | Type      | Description |
|----------------|-----------|-------------|
| `id`           | Integer (PK) | Unique user identifier |
| `external_user_id` | String (nullable, unique) | Optional external system reference |
| `display_name` | String | Display name |
| `token`        | String (unique, hashed) | SHA-256 hash of the user's API token |
| `is_admin`     | Boolean | Marks system administrator users |
| `created_at`   | DateTime | Timestamp of creation |
| `file_quota`   | Integer (nullable) | Max files allowed for that user |
| `file_count`   | Integer (default=0) | Number of uploaded files |

**Relationships:** `memberships` one-to-many → `PartitionMembership`

---

### `partitions`

Represents a logical workspace or "space" that groups files and users.

:::caution
"partition" must be unique across all users as it is used as a partition key in Milvus.
:::

| Column       | Type | Description |
|---------------|------|-------------|
| `id`          | Integer (PK) | Unique partition identifier |
| `partition`   | String (unique, indexed) | Human-readable name / key |
| `created_at`  | DateTime | Timestamp of creation |

**Relationships:**
- `files` one-to-many → `File`
- `memberships` one-to-many → `PartitionMembership`

---

### `files`

Represents an indexed file belonging to a partition.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Internal file identifier |
| `file_id`        | String (indexed) | External file identifier (e.g., hash or ID) |
| `partition_name` | String (FK → `partitions.partition`) | Partition that owns the file |
| `file_metadata`  | JSON | Additional metadata (format, size, etc.) |
| `relationship_id` | String (nullable, indexed) | Groups related documents (e.g., email thread ID, folder path) |
| `parent_id`       | String (nullable, indexed) | Points to hierarchical parent (e.g., parent email) |

**Indexes:**
- `ix_relationship_partition (relationship_id, partition_name)` — enables efficient relationship queries
- `ix_parent_partition (parent_id, partition_name)` — enables efficient ancestor traversal

**Constraints:**
- `UniqueConstraint(file_id, partition_name)` → a file can appear only once per partition
- Composite index `ix_partition_file (partition_name, file_id)` for efficient queries

---

### `partition_memberships`

Defines the many-to-many relationship between users and partitions with role-based access control.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Unique row ID |
| `partition_name` | String (FK → `partitions.partition`, CASCADE) | Partition identifier |
| `user_id`        | Integer (FK → `users.id`, CASCADE) | Linked user |
| `role`           | String | Role: `owner`, `editor`, or `viewer` |
| `added_at`       | DateTime | Timestamp of membership creation |

**Constraints:**
- `UniqueConstraint(partition_name, user_id)` → a user can appear only once per partition
- `CheckConstraint(role IN ('owner','editor','viewer'))` → role validation
- Composite index `ix_user_partition (user_id, partition_name)`

---

### `workspaces`

Groups files within a partition into named subsets for scoped search and chat. See [Workspaces](/openrag/documentation/workspaces/) for full details.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Internal identifier |
| `workspace_id`   | String | Client-facing workspace identifier, unique per partition (`uix_workspace_partition_id` on `(partition_name, workspace_id)`), not globally |
| `partition_name` | String (FK → `partitions.partition`, CASCADE) | Owning partition |
| `display_name`   | String (nullable) | Human-readable name |
| `created_by`     | Integer (FK → `users.id`, SET NULL) | User who created the workspace |
| `created_at`     | DateTime | Timestamp of creation |

**Relationships:** `files` many-to-many → `File` (via `workspace_files`)

---

### `workspace_files`

Join table linking workspaces to files.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Internal identifier |
| `workspace_id`   | Integer (FK → `workspaces.id`, CASCADE) | Workspace reference (the integer PK, since the string `workspace_id` is only unique per partition) |
| `file_id`        | Integer (FK → `files.id`, CASCADE) | File reference (the integer PK, since the string `file_id` is only unique per partition) |

**Constraints:**
- `UniqueConstraint(workspace_id, file_id)` → a file appears at most once per workspace

---

### `jobs`

Durable history of indexing tasks, one row per task. The in-memory Ray `TaskStateManager` is lost when Ray restarts; this table is what keeps a task's outcome visible afterwards.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | String (PK) | Task id, the same one used by `/indexer/task/{task_id}` |
| `partition`      | String | Target partition (no FK constraint, so the history outlives a deleted partition) |
| `file_id`        | String (nullable) | Target file |
| `user_id`        | Integer (FK → `users.id`, SET NULL, nullable) | User who submitted the task |
| `status`         | String | `QUEUED`, `SERIALIZING`, `COMPLETED`, `FAILED` or `CANCELLED` |
| `error`          | String (nullable) | Failure message or traceback, truncated to 8,000 characters |
| `created_at`     | DateTime (tz) | When the task was queued |
| `updated_at`     | DateTime (tz) | Last write to the row |
| `started_at`     | DateTime (tz, nullable) | When a worker picked the task up (queue wait is `started_at - created_at`) |
| `completed_at`   | DateTime (tz, nullable) | When the task settled |

**Constraints:**
- `CheckConstraint ck_jobs_status` → `status` must be a `DocumentStatus` value

**Indexes:**
- `ix_jobs_status_created_at (status, created_at)` — queue views filtered by status, newest first
- `ix_jobs_user_status (user_id, status)` — per-user task listing
- `ix_jobs_settled_at (COALESCE(completed_at, created_at))` — retention sweep
- `ix_jobs_partition_file_id (partition, file_id)` — lookups by file

**Lifecycle:**
- The dispatcher inserts the row as `QUEUED`, or writes `FAILED` if the task could not be submitted
- The indexer worker sets `SERIALIZING` and `started_at` when it starts the task
- The `TaskCompletionTracker` actor writes the final status, `error` and `completed_at` once the task settles
- A terminal row never reopens: once `COMPLETED`, `FAILED` or `CANCELLED`, later writes cannot change its `status`, `error` or `completed_at`
- At startup and every 2 minutes, a non-terminal row whose task the `TaskStateManager` no longer knows, and that has not been updated for 5 minutes, is marked `FAILED` as interrupted by a restart
- Terminal rows are deleted 30 days after they settle

Queue endpoints merge these rows with the live `TaskStateManager` state; while a task is still in memory, the live state wins.

---

## Milvus Schema

Milvus stores document chunks with their vector embeddings. The collection uses dynamic fields for flexible metadata.

```mermaid
erDiagram
    CHUNK {
        int64 _id PK "Auto-generated"
        varchar text "Full-text search enabled"
        float_vector vector "Dense HNSW index"
        sparse_float_vector sparse "BM25 sparse index"
        varchar partition "Partition key"
        varchar file_id "Inverted index"
    }

    DYNAMIC_FIELDS {
        int page "Page number"
        string source "Source path"
        string filename "Display name"
        string chunk_type "text OR table OR image"
        int section_id "Chunk navigation ID"
        int prev_section_id "Previous chunk link"
        int next_section_id "Next chunk link"
    }

    CHUNK ||--o{ DYNAMIC_FIELDS : "has dynamic"
```

**Indexes:**
- **HNSW** on `vector` field - Fast approximate nearest neighbor search with cosine similarity
- **BM25** on `text` field - Keyword-based sparse retrieval
- **Inverted** on `file_id` - Fast filtering by file
- **Partition key** on `partition` - Automatic data isolation by tenant

---

## Database Integration

The two databases are linked by shared identifiers: `file_id` and `partition` exist in both systems.

```mermaid
flowchart LR
    subgraph PG["PostgreSQL"]
        F[files]
        PM[partition_memberships]
    end

    subgraph MV["Milvus"]
        C[Chunks]
    end

    F -- "file_id" --> C
    F -- "partition" --> C
    PM -- "partition" --> C
```

| Data | PostgreSQL | Milvus | Rationale |
|------|:----------:|:------:|-----------|
| Partition metadata | ✓ | - | Referential integrity, access control |
| File inventory | ✓ | - | Single source of truth for uploaded files |
| Workspace membership | ✓ | - | File grouping resolved at query time |
| User accounts & roles | ✓ | - | Authentication, ACID compliance |
| Indexing job history | ✓ | - | Task outcomes survive Ray restarts |
| Document chunks | - | ✓ | Optimized for vector operations |
| Dense embeddings | - | ✓ | HNSW similarity search |
| Sparse embeddings | - | ✓ | BM25 keyword matching |

---

## Operation Flows

### Add File

```mermaid
flowchart LR
    A[Upload] --> B{File exists?}
    B -->|Yes| C[Reject duplicate]
    B -->|No| D[Chunk document]
    D --> E[Generate embeddings]
    E --> F[(Milvus: Insert chunks)]
    F --> G[(PostgreSQL: Record file)]
    G --> H[Done]
```

### Delete File

```mermaid
flowchart LR
    A[Delete request] --> B[(Milvus: Delete chunks)]
    B --> C[(PostgreSQL: Remove file record)]
    C --> D[Done]
```

### Search

```mermaid
flowchart LR
    A[Query] --> B[(PostgreSQL: Check access)]
    B -->|Authorized| C[(Milvus: Hybrid search)]
    C --> D[Rerank results]
    D --> E[Return chunks]
    B -->|Denied| F[403 Forbidden]
```

---

## Access Control

- Roles (`owner`, `editor`, `viewer`) determine what users can do in each partition
- `is_admin` users are privileged globally (admin endpoints, user management)
- `SUPER_ADMIN_MODE=true` allows the global admin to bypass all partition-level restrictions

---

## File Quotas

Limits the number of files a user can upload (indexed files + pending tasks).

- Admins always have unlimited quota and can update quota for a given user
- `DEFAULT_FILE_QUOTA < 0` to disable quota checking (e.g., default `-1`)
- `DEFAULT_FILE_QUOTA >= 0` to set a default quota for all users (note: `0` means users may upload zero files — quota is still enforced)

The default value `DEFAULT_FILE_QUOTA` is -1, meaning that file quota checking is bypassed.

---

## Token Handling

- Tokens are generated at user creation time (`or-<random hex>`)
- Only a **SHA-256 hash** is stored in the database
- During authentication, the incoming Bearer token is hashed and compared with the stored hash
